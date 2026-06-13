"""仓位生命周期规则（吸收四家经典实战：保本第一 + 结构跟踪 + T1/T2 分批）。

纯函数，无 I/O。引擎逐 bar 调用，本模块只产「平仓意图」（ExitIntent），
由引擎翻译成 T+1 卖单（order_id + intended_dt）。

A 股 MVP 只做多（long）：BUY 进场 / SELL 平仓，无做空。

规则：
  3 初始止损   = T0.low − stop_offset（按实际成交价重算 1R，决策 3）
  4 风险单位 1R = entry − stop；T1/T2 由 compute_structural_targets 按结构算（D2/D5）
  5 买入日破线  T1bar 收盘 < stop → 次日开盘全平（BREAKDOWN）
  6 达 target1  high ≥ target1 → 拉保本（止损→入场价，D1）+ 卖 scale_out_pct×original_qty
  6b 达 target2 减仓后 high ≥ target2 → 清剩余（TARGET2，结构量度移动）
  7 移动止损   减仓后按 guard（最近确认 HL）逐级上移，只上不下，地板=入场价（D1）
              ——删除旧的 target1+ε 地板；清盘价可低于 target1，但绝不低于入场价
  8 时间止损   自进场后无新高累计 time_stop_bars 根 → 卖剩余（决策 1）

🔒 移动止损不变量（D1）：达 T1 后 current_stop ≥ 入场价（保本第一）；
之后按结构台阶逐级上移，只上不下。runner 部分可在 ≥ 入场价的任意位清盘。
"""

from __future__ import annotations

from dataclasses import dataclass

from asteria.backtest.types import ExitReason, Fill, Position, PositionStatus
from asteria.data.contracts import DailyBar
from asteria.malf.types import Direction
from asteria.signal.structural import compute_structural_targets


@dataclass(frozen=True)
class RulesConfig:
    """仓位规则参数（来自 config/params_default.toml [backtest]）。"""

    stop_offset: float = 0.02
    target_r: float = 1.0
    scale_out_pct: float = 0.5
    time_stop_bars: int = 8
    trail_method: str = "prev_hl"  # prev_hl / chandelier / atr
    trail_k: float = 3.0
    atr_period: int = 14
    min_risk_pct: float = 0.0  # 最小风险距离占 entry 比例（修复 RR 虚高，与 Signal 一致）
    epsilon: float = 0.01  # 废弃：旧 target1+ε 地板已删，保留兼容


DEFAULT_CONFIG = RulesConfig()


@dataclass
class ExitIntent:
    """平仓意图（引擎翻译成 T+1 卖单）。"""

    reason: ExitReason
    qty: float  # 本次拟卖出数量


def _round2(x: float) -> float:
    return round(x, 2)


def open_position(
    fill: Fill,
    *,
    position_id: str,
    t0_low: float,
    progress_extreme: float | None = None,
    guard: float | None = None,
    direction: Direction = Direction.UP,
    cfg: RulesConfig = DEFAULT_CONFIG,
    signal_candidate_key: str | None = None,
) -> Position:
    """按实际成交价（决策 3）建仓，调 compute_structural_targets 算权威 stop/1R/T1/T2。

    initial_stop = t0_low − stop_offset；1R = entry − stop。
    T1/T2 按发现时捕获的结构价（progress_extreme/guard）算（D2/D5）；
    缺结构价时退回 1R（target1=entry+1R, target2=None）——旧直接调用方仍编译。
    """
    entry = fill.fill_price
    raw_stop = _round2(t0_low - cfg.stop_offset)
    targets = compute_structural_targets(
        entry=entry,
        stop=raw_stop,
        progress_extreme=progress_extreme,
        guard=guard,
        target_r=cfg.target_r,
        min_risk_pct=cfg.min_risk_pct,
    )
    # 用 effective_stop（min_risk 地板后）：与 Signal 裁决一致；退化笔（raw_risk≤0）原样
    stop = targets.effective_stop
    return Position(
        position_id=position_id,
        symbol=fill.symbol,
        direction=direction,
        entry_dt=fill.fill_dt,
        entry_price=entry,
        qty=fill.qty,
        original_qty=fill.qty,
        initial_stop=stop,
        risk_unit_R=targets.risk_unit,
        target1=targets.target1,
        target2=targets.target2,
        current_stop=stop,
        breakeven_armed=False,
        half_exited=False,
        bars_held=0,
        bars_since_new_extreme=0,
        extreme_since_entry=None,
        realized_pnl=0.0,
        status=PositionStatus.OPEN,
        signal_candidate_key=signal_candidate_key,
    )


def _check_trail_method(cfg: RulesConfig) -> None:
    """校验 trail_method；chandelier/atr 留作 M5。"""
    if cfg.trail_method == "prev_hl":
        return
    if cfg.trail_method in ("chandelier", "atr"):
        raise NotImplementedError(
            f"trail_method={cfg.trail_method!r} 留作 M5 调参项，M4 只实现 prev_hl"
        )
    raise ValueError(f"未知 trail_method: {cfg.trail_method!r}")


def _update_tracking(pos: Position, bar: DailyBar) -> None:
    """更新自进场极值 + 无新高计数（时间止损用，决策 1）+ 持有 bar 数。"""
    made_new_extreme: bool
    if pos.extreme_since_entry is None:
        pos.extreme_since_entry = bar.high if pos.direction == Direction.UP else bar.low
        made_new_extreme = True
    elif pos.direction == Direction.UP:
        if bar.high > pos.extreme_since_entry:
            pos.extreme_since_entry = bar.high
            made_new_extreme = True
        else:
            made_new_extreme = False
    else:  # DOWN
        if bar.low < pos.extreme_since_entry:
            pos.extreme_since_entry = bar.low
            made_new_extreme = True
        else:
            made_new_extreme = False

    pos.bars_since_new_extreme = 0 if made_new_extreme else pos.bars_since_new_extreme + 1
    pos.bars_held += 1


def advance(
    pos: Position,
    bar: DailyBar,
    *,
    is_entry_day: bool,
    guard_price: float | None = None,
    cfg: RulesConfig = DEFAULT_CONFIG,
) -> list[ExitIntent]:
    """逐 bar 推进单仓位，返回本 bar 触发的平仓意图（0 或 1 条，T+1 执行）。

    bar 为已收盘的当前 bar；意图由引擎排成下一交易日卖单（T+1）。
    guard_price：本 bar 的 MALF 最近确认 HL（结构跟踪台阶）；缺失则退回 prev_hl（bar.low）。

    检查顺序（D1/D2）：破线 → target1 减半+拉保本 → [减仓后] target2 → 结构移动止损
                       → [减仓前] 初始止损 → 时间止损。
    """
    _update_tracking(pos, bar)
    breakeven = _round2(pos.entry_price)

    # 规则 5：买入日破线（仅 T1，用收盘价；次日开盘全平）
    if is_entry_day:
        if bar.close < pos.initial_stop:
            return [ExitIntent(reason=ExitReason.BREAKDOWN, qty=pos.qty)]
        # 进场当日不做其它平仓检查（刚建仓）
        return []

    # 退化仓位守卫：实际成交价跳空到止损下方（risk_unit_R ≤ 0），进场即已破止损。
    # 此时 target1 = min(前高, entry+1R) 会掉到入场价下方，被规则 6 误判"达标减仓"，
    # 产出无意义的 R=0 半仓。忠实做法：T1/T2 几何已失效，立即按止损全平（次日开盘）。
    if pos.risk_unit_R <= 0:
        return [ExitIntent(reason=ExitReason.STOP, qty=pos.qty)]

    # 规则 6：达 target1 → 拉保本（止损→入场价，D1）+ 减仓
    if not pos.half_exited and bar.high >= pos.target1:
        pos.current_stop = max(pos.current_stop, breakeven)
        pos.breakeven_armed = True
        sell_qty = _round_lot(pos.original_qty * cfg.scale_out_pct)
        # 减仓数量退化为 0（仓位太小）时跳过减仓，直接走后续止损逻辑
        if sell_qty > 0:
            return [ExitIntent(reason=ExitReason.TARGET1, qty=sell_qty)]

    if pos.half_exited:
        # 规则 6b：达 target2（结构量度移动）→ 清剩余
        if pos.target2 is not None and bar.high >= pos.target2:
            return [ExitIntent(reason=ExitReason.TARGET2, qty=pos.qty)]
        # 规则 7：结构移动止损（D1）——地板=入场价（保本），按 guard 逐级上移，只上不下。
        # 破线检查用「prior bars 建立的止损」（≥ 入场价），不含本 bar 的上移，
        # 否则刚减仓即误触发。删除旧 target1+ε 地板：清盘价可低于 target1。
        _check_trail_method(cfg)
        active_stop = max(pos.current_stop, breakeven)
        if bar.low <= active_stop:
            pos.current_stop = active_stop
            return [ExitIntent(reason=ExitReason.TRAILING, qty=pos.qty)]
        # 未破线：按结构台阶（guard）上移，缺 guard 退回 prev_hl（bar.low）。单调不降、≥ 入场价。
        ratchet = guard_price if guard_price is not None else bar.low
        pos.current_stop = max(active_stop, ratchet, breakeven)
    else:
        # 减仓前：初始止损（标准止损，任意持有日）
        if bar.low <= pos.current_stop:
            return [ExitIntent(reason=ExitReason.STOP, qty=pos.qty)]

    # 规则 8：时间止损（自进场后无新高累计 time_stop_bars 根）
    if pos.bars_since_new_extreme >= cfg.time_stop_bars:
        return [ExitIntent(reason=ExitReason.TIME_STOP, qty=pos.qty)]

    return []


def _round_lot(qty: float) -> float:
    """A 股 100 股整手（向下取整到百股）。"""
    return float(int(qty // 100) * 100)
