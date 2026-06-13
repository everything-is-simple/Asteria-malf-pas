"""仓位生命周期规则测试（M4 §2 §10 手算对账核心）。

覆盖规则 3–8 + 移动止损不变量 + R_multiple，全部手算 oracle。
"""

from datetime import date, timedelta

from asteria.backtest.rules import (
    DEFAULT_CONFIG,
    RulesConfig,
    advance,
    open_position,
)
from asteria.backtest.types import ExitReason, Fill, FillRejectReason
from asteria.data.contracts import DailyBar
from asteria.malf.types import Direction

_D0 = date(2025, 1, 10)


def _bar(day_offset: int, *, o: float, h: float, low: float, c: float) -> DailyBar:
    return DailyBar(
        symbol="600000.SH",
        bar_dt=_D0 + timedelta(days=day_offset),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1000.0,
        amount=10000.0,
    )


def _fill(price: float, qty: float = 1000.0, day_offset: int = 0) -> Fill:
    return Fill(
        fill_id="f1",
        order_id="o1",
        symbol="600000.SH",
        fill_dt=_D0 + timedelta(days=day_offset),
        fill_price=price,
        qty=qty,
        reject_reason=FillRejectReason.NONE,
    )


# =========================================================================
# 规则 3+4：初始止损 / 风险单位 / target1（按实际成交价，决策 3）
# =========================================================================
def test_initial_stop_and_risk_unit():
    """entry=10.20（实际成交价），t0_low=10.00，stop_offset=0.02。

    stop = 10.00 - 0.02 = 9.98
    1R   = 10.20 - 9.98 = 0.22
    target1 = 10.20 + 1.0×0.22 = 10.42
    """
    pos = open_position(_fill(10.20), position_id="p1", t0_low=10.00)
    assert pos.initial_stop == 9.98
    assert abs(pos.risk_unit_R - 0.22) < 1e-9
    assert pos.target1 == 10.42
    assert pos.current_stop == 9.98
    assert pos.original_qty == 1000.0


# =========================================================================
# 规则 5：买入日破线 → T2 全平（BREAKDOWN）
# =========================================================================
def test_breakdown_t1_close_below_stop():
    """进场日（T1）收盘 < initial_stop → BREAKDOWN 全平。"""
    pos = open_position(_fill(10.20), position_id="p1", t0_low=10.00)  # stop=9.98
    # T1 bar：收盘 9.95 < 9.98
    bar = _bar(0, o=10.20, h=10.30, low=9.90, c=9.95)
    intents = advance(pos, bar, is_entry_day=True)
    assert len(intents) == 1
    assert intents[0].reason == ExitReason.BREAKDOWN
    assert intents[0].qty == pos.qty


def test_no_breakdown_when_t1_close_above_stop():
    """进场日收盘 ≥ stop → 不破线，持有。"""
    pos = open_position(_fill(10.20), position_id="p1", t0_low=10.00)  # stop=9.98
    bar = _bar(0, o=10.20, h=10.30, low=10.00, c=10.10)
    intents = advance(pos, bar, is_entry_day=True)
    assert intents == []


# =========================================================================
# 规则 6：达 target1 减仓 50%
# =========================================================================
def test_target1_scale_out_half_and_arms_breakeven():
    """high ≥ target1 且未减仓 → 卖 original_qty × 0.5，同时拉保本（D1）。"""
    pos = open_position(_fill(10.20), position_id="p1", t0_low=10.00)  # entry=10.20 target1=10.42
    bar = _bar(1, o=10.30, h=10.50, low=10.25, c=10.40)  # high 10.50 ≥ 10.42
    intents = advance(pos, bar, is_entry_day=False)
    assert len(intents) == 1
    assert intents[0].reason == ExitReason.TARGET1
    assert intents[0].qty == 500.0  # 1000 × 0.5
    # D1：达 T1 即拉保本——止损升到入场价，breakeven_armed 置位
    assert pos.breakeven_armed is True
    assert pos.current_stop == round(pos.entry_price, 2)  # 10.20


# =========================================================================
# 规则 7：移动止损不变量（D1 保本第一 + 结构跟踪，删除旧 target1+ε 地板）
# =========================================================================
def test_trailing_floor_is_breakeven_not_target1():
    """减仓后 current_stop 地板 = 入场价（保本），允许低于 target1。

    经典共识（LanceBeggs/Volman/达瓦斯）：保本第一，清盘价可低于 T1，但绝不低于入场价。
    """
    pos = open_position(_fill(10.20), position_id="p1", t0_low=10.00)  # entry=10.20 target1=10.42
    pos.half_exited = True
    pos.qty = 500.0
    pos.current_stop = pos.entry_price  # 已拉保本
    # 一根未破保本、低点低于 target1 的 bar：止损按结构（无 guard → prev_hl=bar.low）上移
    bar = _bar(2, o=10.45, h=10.60, low=10.30, c=10.50)
    advance(pos, bar, is_entry_day=False)
    # 地板是入场价、不是 target1+ε；当前止损 ≥ 入场价，且可低于 target1
    assert pos.current_stop >= pos.entry_price
    assert pos.current_stop < pos.target1  # 10.30 < 10.42——清盘价可低于 T1


def test_trailing_ratchet_uses_guard_step():
    """减仓后传入 guard_price → 止损按结构台阶上移（只上不下，≥ 入场价）。"""
    pos = open_position(_fill(10.20), position_id="p1", t0_low=10.00)  # entry=10.20
    pos.half_exited = True
    pos.qty = 500.0
    pos.current_stop = pos.entry_price  # 10.20
    bar = _bar(2, o=10.60, h=10.80, low=10.50, c=10.70)
    # guard=10.45（最近确认 HL）高于入场价 → 止损上移到 10.45
    advance(pos, bar, is_entry_day=False, guard_price=10.45)
    assert pos.current_stop == 10.45
    # 下一根 guard 回落到 10.30（< 当前止损）→ 只上不下，仍 10.45
    advance(pos, _bar(3, o=10.55, h=10.65, low=10.50, c=10.60), is_entry_day=False, guard_price=10.30)
    assert pos.current_stop == 10.45


def test_trailing_exit_when_low_hits_stop():
    """减仓后 low ≤ current_stop（保本地板）→ TRAILING 清剩余仓位。"""
    pos = open_position(_fill(10.20), position_id="p1", t0_low=10.00)  # entry=10.20
    pos.half_exited = True
    pos.qty = 500.0
    pos.current_stop = 10.30  # 已跟踪上移到 10.30
    # low 10.25 ≤ 10.30 → 触发
    bar = _bar(3, o=10.32, h=10.35, low=10.25, c=10.28)
    intents = advance(pos, bar, is_entry_day=False)
    assert len(intents) == 1
    assert intents[0].reason == ExitReason.TRAILING
    assert intents[0].qty == 500.0


# =========================================================================
# 规则 6b：达 target2（结构量度移动）→ 清剩余
# =========================================================================
def test_target2_exit_when_high_hits():
    """减仓后 high ≥ target2 → TARGET2 清剩余仓位。"""
    pos = open_position(
        _fill(10.20), position_id="p1", t0_low=10.00,
        progress_extreme=11.00, guard=10.00,
    )  # entry=10.20；target2=11.00+(11.00-10.00)=12.00
    assert pos.target2 == 12.00
    pos.half_exited = True
    pos.qty = 500.0
    pos.current_stop = pos.entry_price
    bar = _bar(4, o=11.90, h=12.10, low=11.80, c=12.00)  # high 12.10 ≥ 12.00
    intents = advance(pos, bar, is_entry_day=False)
    assert len(intents) == 1
    assert intents[0].reason == ExitReason.TARGET2
    assert intents[0].qty == 500.0


# =========================================================================
# 规则：减仓前初始止损
# =========================================================================
def test_initial_stop_exit_before_target():
    """未减仓时 low ≤ current_stop → STOP 全平。"""
    pos = open_position(_fill(10.20), position_id="p1", t0_low=10.00)  # stop=9.98
    bar = _bar(1, o=10.10, h=10.15, low=9.95, c=10.00)  # low 9.95 ≤ 9.98
    intents = advance(pos, bar, is_entry_day=False)
    assert len(intents) == 1
    assert intents[0].reason == ExitReason.STOP
    assert intents[0].qty == 1000.0


# =========================================================================
# 退化仓位守卫：T1 实际成交价跳空到止损下方（risk_unit_R ≤ 0）
# =========================================================================
def test_degenerate_position_exits_at_stop():
    """进场价跳空到止损下方（risk_unit_R ≤ 0）→ 非进场日立即 STOP 全平。

    计划 entry≈6.10/stop=6.07，但 T1 实际开盘跳空到 6.05 < stop 6.07：
      stop = t0_low - 0.02 = 6.07；risk_unit = 6.05 - 6.07 = -0.02 ≤ 0
      target1 = min(前高, entry+1R) = min(6.30, 6.03) = 6.03 < 入场价
    若不守卫，bar.high≥6.03 会误判"达标减仓"产出 R=0 半仓。守卫令其直接按止损退出。
    """
    pos = open_position(
        _fill(6.05), position_id="p1", t0_low=6.07,
        progress_extreme=6.30, guard=6.00,
    )
    assert pos.risk_unit_R <= 0  # 退化：进场即破止损
    # 非进场日：即使本 bar 创新高，也不减仓，直接 STOP 全平
    bar = _bar(1, o=6.05, h=6.40, low=6.02, c=6.20)
    intents = advance(pos, bar, is_entry_day=False)
    assert len(intents) == 1
    assert intents[0].reason == ExitReason.STOP
    assert intents[0].qty == pos.qty
    assert not pos.half_exited  # 绝不减仓


# =========================================================================
# 规则 8：时间止损（自进场后无新高累计 N 根，决策 1）
# =========================================================================
def test_time_stop_after_no_new_high():
    """time_stop_bars=3：进场后连续 3 根无新高 → TIME_STOP。"""
    cfg = RulesConfig(time_stop_bars=3)
    pos = open_position(_fill(10.20), position_id="p1", t0_low=10.00, cfg=cfg)
    # 第 1 根（非进场日）：创新高 10.40，重置计数（high>extreme）
    advance(pos, _bar(1, o=10.20, h=10.40, low=10.20, c=10.35), is_entry_day=False, cfg=cfg)
    assert pos.bars_since_new_extreme == 0
    # 第 2–4 根：无新高（high < 10.40），计数累加 1,2,3
    advance(pos, _bar(2, o=10.35, h=10.38, low=10.20, c=10.30), is_entry_day=False, cfg=cfg)
    assert pos.bars_since_new_extreme == 1
    advance(pos, _bar(3, o=10.30, h=10.36, low=10.20, c=10.28), is_entry_day=False, cfg=cfg)
    assert pos.bars_since_new_extreme == 2
    intents = advance(
        pos, _bar(4, o=10.28, h=10.35, low=10.20, c=10.30), is_entry_day=False, cfg=cfg
    )
    assert pos.bars_since_new_extreme == 3
    assert len(intents) == 1
    assert intents[0].reason == ExitReason.TIME_STOP


def test_new_high_resets_time_stop_counter():
    """创新高重置无新高计数。"""
    cfg = RulesConfig(time_stop_bars=3)
    pos = open_position(_fill(10.20), position_id="p1", t0_low=10.00, cfg=cfg)
    advance(pos, _bar(1, o=10.20, h=10.40, low=10.20, c=10.35), is_entry_day=False, cfg=cfg)
    advance(pos, _bar(2, o=10.35, h=10.38, low=10.20, c=10.30), is_entry_day=False, cfg=cfg)
    assert pos.bars_since_new_extreme == 1
    # 创新高 10.50 → 重置
    advance(pos, _bar(3, o=10.40, h=10.50, low=10.30, c=10.45), is_entry_day=False, cfg=cfg)
    assert pos.bars_since_new_extreme == 0


# =========================================================================
# trail_method 未实现项
# =========================================================================
def test_chandelier_atr_not_implemented():
    """chandelier/atr 留 M5，应抛 NotImplementedError。"""
    import pytest

    for method in ("chandelier", "atr"):
        cfg = RulesConfig(trail_method=method)
        pos = open_position(_fill(10.20), position_id="p1", t0_low=10.00, cfg=cfg)
        pos.half_exited = True
        pos.qty = 500.0
        with pytest.raises(NotImplementedError):
            advance(pos, _bar(1, o=10.45, h=10.60, low=10.40, c=10.50), is_entry_day=False, cfg=cfg)
