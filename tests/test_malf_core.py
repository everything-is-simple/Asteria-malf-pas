"""MALF Core 状态机测试：用合成 OHLC 序列验证 D/T/O 规则。

构造可手算的 pivot 序列，断言初始化、break（O3 严格）、transition 双边界、
new wave 双条件确认（T6）的行为与规范一致。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

# 让测试无需安装即可导入 src 包
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from asteria.data.contracts import DailyBar  # noqa: E402
from asteria.malf.core import CoreEngine  # noqa: E402
from asteria.malf.types import Direction, SystemState  # noqa: E402

_D0 = date(2020, 1, 1)


def _bar(i: int, high: float, low: float) -> DailyBar:
    """构造第 i 根 bar；open/close 落在 [low, high] 内（不影响结构）。"""
    mid = round((high + low) / 2, 2)
    return DailyBar(
        symbol="TEST",
        bar_dt=_D0 + timedelta(days=i),
        open=mid,
        high=high,
        low=low,
        close=mid,
        volume=1.0,
        amount=1.0,
    )


def _run(seq: list[tuple[float, float]], k: int = 1) -> CoreEngine:
    bars = [_bar(i, hi, lo) for i, (hi, lo) in enumerate(seq)]
    eng = CoreEngine("TEST", k=k)
    eng.run(bars)
    return eng


def test_initial_up_wave_forms_on_H0_L1_H2():
    """H0 -> L1 -> H2 且 H2>H0：确认 initial up wave，system_state=up_alive。

    k=1 分形：要确认一个 high pivot，需前后各 1 根更低 high；low 同理。
    设计序列让 pivot 依次为 H0(bar1)、L1(bar3)、H2(bar5)。
    """
    seq = [
        (10.0, 9.0),   # 0
        (12.0, 11.0),  # 1 H0=12 (前0低, 后2低)
        (11.5, 10.0),  # 2
        (11.0, 8.0),   # 3 L1=8  (前2高, 后4高)
        (11.8, 9.5),   # 4
        (14.0, 12.5),  # 5 H2=14 > H0=12
        (13.0, 12.0),  # 6
    ]
    eng = _run(seq, k=1)
    assert eng.system_state == SystemState.UP_ALIVE
    assert eng.active_wave is not None
    assert eng.active_wave.direction == Direction.UP
    # guard = L1 = 8.0
    assert eng.active_wave.current_guard_price == 8.0
    # progress = H2 = 14.0
    assert eng.active_wave.progress_extreme_price == 14.0


def test_no_wave_when_H2_not_above_H0():
    """H2 <= H0：不成 wave，保持 uninitialized，绝不 break/transition（O6）。"""
    seq = [
        (10.0, 9.0),
        (12.0, 11.0),  # H0=12
        (11.5, 10.0),
        (11.0, 8.0),   # L1=8
        (11.8, 9.5),
        (12.0, 10.5),  # H2=12 == H0，不满足 >（严格）
        (11.0, 10.0),
    ]
    eng = _run(seq, k=1)
    assert eng.system_state == SystemState.UNINITIALIZED
    assert len(eng._breaks) == 0
    assert len(eng._transitions) == 0


def test_break_terminates_up_wave_and_opens_transition():
    """up wave 后，bar_low 严格跌破 guard → break + transition（D10/D13）。"""
    seq = [
        (10.0, 9.0),
        (12.0, 11.0),  # H0=12
        (11.5, 10.0),
        (11.0, 8.0),   # L1=8 (guard)
        (11.8, 9.5),
        (14.0, 12.5),  # H2=14 -> up_alive
        (13.0, 12.0),
        (12.0, 7.99),  # 跌破 guard 8.0（7.99 < 8.0）→ break
        (11.0, 10.0),
    ]
    eng = _run(seq, k=1)
    assert len(eng._breaks) == 1
    brk = eng._breaks[0]
    assert brk.old_direction == Direction.UP
    assert brk.broken_guard_price == 8.0
    assert eng.system_state == SystemState.TRANSITION
    t = eng._transitions[0]
    # D13: up wave break → boundary_high=old final HH(14), boundary_low=broken HL(8)
    assert t.boundary_high == 14.0
    assert t.boundary_low == 8.0


def test_equality_does_not_break():
    """O3：bar_low == guard_price 不构成 break。"""
    seq = [
        (10.0, 9.0),
        (12.0, 11.0),
        (11.5, 10.0),
        (11.0, 8.0),   # guard=8
        (11.8, 9.5),
        (14.0, 12.5),  # up_alive
        (13.0, 12.0),
        (12.0, 8.0),   # low==8 == guard，不破
        (12.5, 11.0),
    ]
    eng = _run(seq, k=1)
    assert len(eng._breaks) == 0
    assert eng.system_state == SystemState.UP_ALIVE
