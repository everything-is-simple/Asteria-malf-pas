"""回测事件循环引擎（docs/02-module-design/BACKTEST_DESIGN.md §5 §2 时间轴 + 无未来函数铁律）。

🔒 回测层是唯一拥有仓位/订单/成交/盈亏语义的层。

逐 bar 严格因果（§5 六步）：
  1. 结算 pending orders（本 bar open 集合竞价撮合 + 涨跌停检查）
  2. 更新 open Position（advance：1R 进度 / target1 减半 / 移动止损 / 时间止损）
  3. 平仓意图 → 下一交易日卖单（T+1）
  4. 读本 bar PASCoreSnapshot → Signal accept/reject（只到本 bar，无未来函数）
  5. accept → 生成下一交易日买单（T+1）
  6. 记录组合净值快照

无未来函数：扫描只读 ≤ bar_dt 数据；进场永远在发现日的下一交易日 open。
PAS 快照由 runner 预算后按 bar_dt 索引（方案 A），引擎只读 snap_by_dt[bar_dt]。

决策落地：
  1 时间止损 = 自进场后无新高累计 time_stop_bars 根（在 rules.advance）
  3 引擎按实际 fill price 重算权威 stop/1R/target1（在 rules.open_position）
  4 减仓后合并为一行混合 Trade（avg_exit_price + 原始 qty）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from asteria.backtest import broker as broker_mod
from asteria.backtest import rules as rules_mod
from asteria.backtest.broker import BrokerConfig
from asteria.backtest.metrics import BtMetrics, compute_metrics
from asteria.backtest.rules import RulesConfig
from asteria.backtest.types import (
    EquityPoint,
    ExitReason,
    Fill,
    Order,
    OrderReason,
    OrderType,
    Position,
    PositionStatus,
    Side,
    StructuralLevels,
    Trade,
)
from asteria.data.contracts import DailyBar
from asteria.malf.types import Direction
from asteria.pas.types import PASCoreSnapshot, Posture, SetupFamily
from asteria.signal.engine import judge
from asteria.signal.types import (
    DEFAULT_CONFIG as SIGNAL_DEFAULT,
)
from asteria.signal.types import (
    SignalCandidate,
    SignalConfig,
    SignalDecision,
)

# family 优先序（select_candidate 平手时的确定性 tie-break）
_FAMILY_ORDER: tuple[SetupFamily, ...] = (
    SetupFamily.TST,
    SetupFamily.BOF,
    SetupFamily.BPB,
    SetupFamily.PB,
    SetupFamily.CPB,
)
# posture 优先：favored > allowed
_POSTURE_RANK = {Posture.FAVORED: 0, Posture.ALLOWED: 1}

_FAMILY_ATTR = {
    SetupFamily.TST: "tst_posture",
    SetupFamily.BOF: "bof_posture",
    SetupFamily.BPB: "bpb_posture",
    SetupFamily.PB: "pb_posture",
    SetupFamily.CPB: "cpb_posture",
}


@dataclass
class SymbolData:
    """单标的回测输入（qfq 结构价 + raw 限价 + 已派生 PAS 快照）。

    qfq_bars 与 snaps 逐 bar 一一对应（同序、同 bar_dt）；raw_bars 用于涨跌停限价。
    """

    symbol: str
    qfq_bars: list[DailyBar]
    raw_bars: list[DailyBar]
    snaps: list[PASCoreSnapshot]
    cores: list[StructuralLevels] = field(default_factory=list)  # MALF 结构价，1:1 对应 snaps
    board: str = "main"
    is_st: bool = False


@dataclass
class EngineConfig:
    """引擎聚合配置。"""

    signal: SignalConfig = field(default_factory=lambda: SIGNAL_DEFAULT)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    initial_capital: float = 1_000_000.0
    position_pct_per_trade: float = 0.1
    # 大盘趋势过滤（决策 B/C）：启用时大盘 ∈ bear_states 停开新多单（已持仓不强平）
    market_filter_enabled: bool = False
    market_index_symbol: str = "000300.SH"
    bear_states: tuple[str, ...] = ("down_alive",)


@dataclass
class BacktestRunResult:
    """单次回测输出。"""

    run_id: str
    group_name: str
    start_dt: date | None
    end_dt: date | None
    trades: list[Trade]
    equity_curve: list[EquityPoint]
    signal_candidates: list[SignalCandidate]
    metrics: BtMetrics


def _round_lot(qty: float) -> float:
    """A 股 100 股整手（向下取整到百股）。"""
    return float(int(qty // 100) * 100)


class BacktestEngine:
    """逐 bar 事件循环（多标的可跑，单标的验收）。"""

    def __init__(
        self,
        *,
        cfg: EngineConfig | None = None,
        source_run_id: str = "adhoc",
        group_name: str = "adhoc",
    ) -> None:
        self.cfg = cfg or EngineConfig()
        self.source_run_id = source_run_id
        self.group_name = group_name
        # 状态
        self.cash: float = self.cfg.initial_capital
        self.open_positions: dict[str, Position] = {}  # symbol → Position（每标的最多一仓）
        self.pending_orders: list[Order] = []
        self.trades: list[Trade] = []
        self.equity_curve: list[EquityPoint] = []
        self.signal_candidates: list[SignalCandidate] = []
        # 内部：position_id → 出场明细（构造混合 Trade，决策 4）
        self._exits: dict[str, list[tuple[date, float, float, ExitReason]]] = {}
        self._market_regime: dict[date, str] = {}
        self._pos_seq = 0
        self._order_seq = 0
        self._cand_seq = 0

    # ---------------------------------------------------------------- run
    def run(
        self,
        symbols_data: list[SymbolData],
        *,
        start_dt: date | None = None,
        end_dt: date | None = None,
        market_regime_by_dt: dict[date, str] | None = None,
    ) -> BacktestRunResult:
        self._market_regime = market_regime_by_dt or {}
        sd_by_symbol = {sd.symbol: sd for sd in symbols_data}
        # 每标的索引
        qfq_by: dict[str, dict[date, DailyBar]] = {}
        raw_by: dict[str, dict[date, DailyBar]] = {}
        snap_by: dict[str, dict[date, PASCoreSnapshot]] = {}
        core_by: dict[str, dict[date, StructuralLevels]] = {}
        prev_raw_close: dict[str, dict[date, float]] = {}
        for sd in symbols_data:
            qfq_by[sd.symbol] = {b.bar_dt: b for b in sd.qfq_bars}
            raw_by[sd.symbol] = {b.bar_dt: b for b in sd.raw_bars}
            snap_by[sd.symbol] = {s.bar_dt: s for s in sd.snaps}
            core_by[sd.symbol] = {c.bar_dt: c for c in sd.cores}
            raw_sorted = sorted(sd.raw_bars, key=lambda b: b.bar_dt)
            pmap: dict[date, float] = {}
            for i, b in enumerate(raw_sorted):
                if i > 0:
                    pmap[b.bar_dt] = raw_sorted[i - 1].close
            prev_raw_close[sd.symbol] = pmap

        # 主日历：所有标的 qfq bar 日期并集（升序），裁剪到 [start, end]
        all_dts = sorted({b.bar_dt for sd in symbols_data for b in sd.qfq_bars})
        calendar = [
            d for d in all_dts
            if (start_dt is None or d >= start_dt) and (end_dt is None or d <= end_dt)
        ]
        # 下一交易日映射（用于 T+1 挂单）
        next_dt = {calendar[i]: calendar[i + 1] for i in range(len(calendar) - 1)}

        for bar_dt in calendar:
            # 1. 结算 pending（本 bar open 撮合）
            self._settle(bar_dt, sd_by_symbol, qfq_by, raw_by, prev_raw_close)

            # 2+3. 更新 open 仓位 → 平仓意图排成 T+1 卖单
            self._advance_positions(bar_dt, next_dt, qfq_by, core_by)

            # 4+5. 驱动 Signal（读本 bar 快照）→ accept 排成 T+1 买单
            self._scan_signals(bar_dt, next_dt, sd_by_symbol, snap_by, qfq_by, core_by)

            # 6. 记录组合净值
            self._snapshot_equity(bar_dt, qfq_by)

        metrics = compute_metrics(
            self.trades,
            self.equity_curve,
            initial_capital=self.cfg.initial_capital,
        )
        return BacktestRunResult(
            run_id=self.source_run_id,
            group_name=self.group_name,
            start_dt=calendar[0] if calendar else None,
            end_dt=calendar[-1] if calendar else None,
            trades=self.trades,
            equity_curve=self.equity_curve,
            signal_candidates=self.signal_candidates,
            metrics=metrics,
        )

    # ----------------------------------------------------------- step 1
    def _settle(
        self,
        bar_dt: date,
        sd_by_symbol: dict[str, SymbolData],
        qfq_by: dict[str, dict[date, DailyBar]],
        raw_by: dict[str, dict[date, DailyBar]],
        prev_raw_close: dict[str, dict[date, float]],
    ) -> None:
        due = [o for o in self.pending_orders if o.intended_dt == bar_dt]
        # 未到期的留下；到期的本 bar 处理后移除（MVP 不顺延）
        self.pending_orders = [o for o in self.pending_orders if o.intended_dt != bar_dt]

        for order in due:
            qfq_bar = qfq_by[order.symbol].get(bar_dt)
            raw_bar = raw_by[order.symbol].get(bar_dt)
            if qfq_bar is None or raw_bar is None:
                continue  # 本标的本 bar 停牌/无数据 → 意图作废
            prev_close = prev_raw_close[order.symbol].get(bar_dt)
            if prev_close is None:
                continue
            sd = sd_by_symbol[order.symbol]
            up, down = broker_mod.price_limits(
                prev_close, board=sd.board, is_st=sd.is_st, cfg=self.cfg.broker
            )
            fill = broker_mod.try_fill(
                order,
                open_qfq=qfq_bar.open,
                open_raw=raw_bar.open,
                up_limit=up,
                down_limit=down,
            )
            if not fill.filled:
                continue  # 涨跌停/停牌 → 撮合失败，意图作废
            if order.side == Side.BUY:
                self._on_buy_fill(order, fill)
            else:
                self._on_sell_fill(order, fill, bar_dt)

    def _on_buy_fill(self, order: Order, fill: Fill) -> None:
        cost = fill.fill_price * fill.qty
        self.cash -= cost
        self._pos_seq += 1
        pos = rules_mod.open_position(
            fill,
            position_id=f"p{self._pos_seq}",
            t0_low=order.t0_low if order.t0_low is not None else fill.fill_price,
            progress_extreme=order.struct_progress_extreme,
            guard=order.struct_guard,
            direction=Direction.UP,
            cfg=self.cfg.rules,
            signal_candidate_key=order.signal_candidate_key,
        )
        self.open_positions[order.symbol] = pos
        self._exits[pos.position_id] = []

    def _on_sell_fill(self, order: Order, fill: Fill, bar_dt: date) -> None:
        pos = self.open_positions.get(order.symbol)
        if pos is None or pos.position_id != order.position_id:
            return
        sell_qty = min(fill.qty, pos.qty)
        proceeds = fill.fill_price * sell_qty
        self.cash += proceeds
        reason = _order_reason_to_exit(order.reason)
        self._exits[pos.position_id].append((bar_dt, fill.fill_price, sell_qty, reason))
        pos.qty -= sell_qty
        if order.reason == OrderReason.TARGET1:
            pos.half_exited = True
        if pos.qty <= 0:
            self._finalize_trade(pos)
            del self.open_positions[order.symbol]

    def _finalize_trade(self, pos: Position) -> None:
        """减仓后合并为一行混合 Trade（决策 4）。"""
        exits = self._exits.pop(pos.position_id, [])
        total_qty = sum(q for _, _, q, _ in exits)
        if total_qty <= 0:
            return
        exit_value = sum(p * q for _, p, q, _ in exits)
        avg_exit = exit_value / total_qty
        realized = sum((p - pos.entry_price) * q for _, p, q, _ in exits)
        denom = pos.risk_unit_R * pos.original_qty
        r_mult = realized / denom if denom > 0 else 0.0
        exit_dt = exits[-1][0]
        exit_reason = exits[-1][3]  # 最终一笔出场的归因
        pos.status = PositionStatus.CLOSED
        self.trades.append(
            Trade(
                trade_id=f"t{len(self.trades) + 1}",
                symbol=pos.symbol,
                entry_dt=pos.entry_dt,
                exit_dt=exit_dt,
                entry_price=pos.entry_price,
                avg_exit_price=round(avg_exit, 4),
                qty=pos.original_qty,
                realized_pnl=round(realized, 4),
                R_multiple=round(r_mult, 4),
                exit_reason=exit_reason,
                signal_candidate_key=pos.signal_candidate_key,
            )
        )

    # --------------------------------------------------------- step 2+3
    def _advance_positions(
        self,
        bar_dt: date,
        next_dt: dict[date, date],
        qfq_by: dict[str, dict[date, DailyBar]],
        core_by: dict[str, dict[date, StructuralLevels]],
    ) -> None:
        nxt = next_dt.get(bar_dt)
        for symbol, pos in list(self.open_positions.items()):
            bar = qfq_by[symbol].get(bar_dt)
            if bar is None:
                continue  # 停牌：不推进
            is_entry_day = pos.entry_dt == bar_dt
            # 结构跟踪台阶：本 bar 的 guard（最近确认 HL），缺失则 advance 内退回 prev_hl
            core = core_by.get(symbol, {}).get(bar_dt)
            guard_price = core.guard_price if core is not None else None
            intents = rules_mod.advance(
                pos,
                bar,
                is_entry_day=is_entry_day,
                guard_price=guard_price,
                cfg=self.cfg.rules,
            )
            if not intents or nxt is None:
                continue
            for it in intents:
                self._order_seq += 1
                self.pending_orders.append(
                    Order(
                        order_id=f"o{self._order_seq}",
                        symbol=symbol,
                        side=Side.SELL,
                        order_type=OrderType.MOO,
                        intended_dt=nxt,  # T+1 卖出
                        reason=_exit_to_order_reason(it.reason),
                        qty=it.qty,
                        position_id=pos.position_id,
                    )
                )

    # --------------------------------------------------------- step 4+5
    def _scan_signals(
        self,
        bar_dt: date,
        next_dt: dict[date, date],
        sd_by_symbol: dict[str, SymbolData],
        snap_by: dict[str, dict[date, PASCoreSnapshot]],
        qfq_by: dict[str, dict[date, DailyBar]],
        core_by: dict[str, dict[date, StructuralLevels]],
    ) -> None:
        nxt = next_dt.get(bar_dt)
        if nxt is None:
            return  # 最后一根 bar 无次日，不进场
        # 大盘趋势过滤（决策 B/C）：大盘 ∈ bear_states 时停开新多单（已持仓不强平，决策 D）。
        # 只读 regime[bar_dt]（无未来函数）；regime 空 = 不过滤。
        if self.cfg.market_filter_enabled and self._market_regime:
            if self._market_regime.get(bar_dt) in self.cfg.bear_states:
                return
        equity = self._equity(bar_dt, qfq_by)
        # 收集本 bar 候选（确定性按 reward_risk 降序竞价现金）
        accepts: list[tuple[SignalCandidate, str, float]] = []
        for symbol, sd in sd_by_symbol.items():
            if symbol in self.open_positions:
                continue  # 已有仓位
            if any(o.symbol == symbol and o.side == Side.BUY for o in self.pending_orders):
                continue  # 已挂买单
            snap = snap_by[symbol].get(bar_dt)
            t0_bar = qfq_by[symbol].get(bar_dt)
            if snap is None or t0_bar is None:
                continue
            core = core_by.get(symbol, {}).get(bar_dt)
            cand = self._select_candidate(snap, t0_bar, core)
            if cand is None:
                continue
            self._cand_seq += 1
            key = f"c{self._cand_seq}"
            cand.source_run_id = self.source_run_id
            self.signal_candidates.append(cand)
            if cand.decision == SignalDecision.ACCEPT:
                accepts.append((cand, key, cand.reward_risk or 0.0))

        accepts.sort(key=lambda x: x[2], reverse=True)
        for cand, key, _rr in accepts:
            t0_bar = qfq_by[cand.symbol][bar_dt]
            core = core_by.get(cand.symbol, {}).get(bar_dt)
            budget = equity * self.cfg.position_pct_per_trade
            expected_entry = cand.planned_entry or t0_bar.close
            qty = _round_lot(budget / expected_entry) if expected_entry > 0 else 0.0
            if qty <= 0 or qty * expected_entry > self.cash:
                continue  # 现金不足或仓位退化
            self._order_seq += 1
            self.pending_orders.append(
                Order(
                    order_id=f"o{self._order_seq}",
                    symbol=cand.symbol,
                    side=Side.BUY,
                    order_type=OrderType.MOO,
                    intended_dt=nxt,  # T+1 进场
                    reason=OrderReason.ENTRY,
                    qty=qty,
                    limit_ref=expected_entry,
                    signal_candidate_key=key,
                    t0_low=t0_bar.low,
                    # 发现时捕获结构价 → 建仓时按实际 fill 重算权威 T1/T2（决策 3）
                    struct_progress_extreme=core.progress_extreme_price if core else None,
                    struct_guard=core.guard_price if core else None,
                )
            )

    def _select_candidate(
        self, snap: PASCoreSnapshot, t0_bar: DailyBar, core: StructuralLevels | None
    ) -> SignalCandidate | None:
        """选本 bar 最优 family 裁决。

        只在「至少一族 posture ∈ accepted_postures」时返回候选（真实机会）；
        否则 None（不记录满屏 posture_not_allowed）。
        favored 优先于 allowed，平手按 family 固定序。
        结构价（progress_extreme/guard）穿给 judge 算可变 RR 与结构 T1/T2。
        """
        cfg = self.cfg.signal
        considered: list[tuple[int, int, SetupFamily]] = []
        for rank, fam in enumerate(_FAMILY_ORDER):
            if fam not in cfg.accept_families:
                continue
            posture = getattr(snap, _FAMILY_ATTR[fam])
            if posture in cfg.accepted_postures:
                considered.append((_POSTURE_RANK.get(posture, 9), rank, fam))
        if not considered:
            return None
        considered.sort()
        chosen_family = considered[0][2]
        # 计划值用 T0 close 作预期开盘代理（因果：≤ bar_dt）
        return judge(
            snap,
            family=chosen_family,
            expected_entry=t0_bar.close,
            t0_low=t0_bar.low,
            structural_target=core.progress_extreme_price if core else None,
            structural_guard=core.guard_price if core else None,
            life_state=core.life_state if core else None,
            cfg=cfg,
            tradable=True,
            discover_dt=snap.bar_dt,
            source_run_id=self.source_run_id,
        )

    # ----------------------------------------------------------- step 6
    def _equity(self, bar_dt: date, qfq_by: dict[str, dict[date, DailyBar]]) -> float:
        total = self.cash
        for symbol, pos in self.open_positions.items():
            bar = qfq_by[symbol].get(bar_dt)
            mark = bar.close if bar is not None else pos.entry_price
            total += pos.qty * mark
        return total

    def _snapshot_equity(
        self, bar_dt: date, qfq_by: dict[str, dict[date, DailyBar]]
    ) -> None:
        self.equity_curve.append(
            EquityPoint(
                bar_dt=bar_dt,
                equity=round(self._equity(bar_dt, qfq_by), 4),
                cash=round(self.cash, 4),
                open_positions=len(self.open_positions),
            )
        )


# ----------------------------------------------------------------- helpers
_EXIT_TO_ORDER = {
    ExitReason.STOP: OrderReason.STOP,
    ExitReason.TARGET1: OrderReason.TARGET1,
    ExitReason.TARGET2: OrderReason.TARGET2,
    ExitReason.TRAILING: OrderReason.TRAILING,
    ExitReason.TIME_STOP: OrderReason.TIME_STOP,
    ExitReason.BREAKDOWN: OrderReason.BREAKDOWN,
}
_ORDER_TO_EXIT = {v: k for k, v in _EXIT_TO_ORDER.items()}


def _exit_to_order_reason(r: ExitReason) -> OrderReason:
    return _EXIT_TO_ORDER[r]


def _order_reason_to_exit(r: OrderReason) -> ExitReason:
    return _ORDER_TO_EXIT[r]
