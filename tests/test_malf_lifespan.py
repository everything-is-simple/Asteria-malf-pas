"""MALF-Lifespan 测试：计数 / rank 单调性 / life_state / birth / transition 不变量。

沿用 test_malf_core.py 的合成 OHLC 序列风格（k=1 可手算）。
验收点（计划）：
- new_count / no_new_span 逐 bar 对账。
- transition_span 不并入新波 no_new_span。
- rank 单调性 sanity（固定 peer 分布）。
- life_state 判定顺序（terminated→terminal 优先）。
- birth_type：initial / same / opposite 三类。
- transition 期 direction = old_direction（L-T5）。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from asteria.data.contracts import DailyBar  # noqa: E402
from asteria.malf.core import CoreEngine, CoreRunResult  # noqa: E402
from asteria.malf.lifespan import (  # noqa: E402
    LifespanConfig,
    _PeerSample,
    _WaveFinalStat,
    _percentile_rank,
    compute_wave_positions,
)
from asteria.malf.types import (  # noqa: E402
    BirthType,
    Direction,
    LifeState,
    SystemState,
    WaveCoreState,
)

_D0 = date(2020, 1, 1)


def _bar(i: int, high: float, low: float) -> DailyBar:
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


def _run(seq: list[tuple[float, float]], k: int = 1) -> tuple[list[DailyBar], CoreRunResult]:
    bars = [_bar(i, hi, lo) for i, (hi, lo) in enumerate(seq)]
    eng = CoreEngine("TEST", k=k)
    result = eng.run(bars)
    return bars, result


def _positions(seq: list[tuple[float, float]], k: int = 1):
    bars, result = _run(seq, k=k)
    positions = compute_wave_positions(result, bars, symbol="TEST")
    return bars, result, positions


# =========================================================================
# percentile_rank 单调性（最干净的 sanity check）
# =========================================================================
def test_percentile_rank_empty_is_none():
    assert _percentile_rank([], 5) is None


def test_percentile_rank_monotonic_nondecreasing():
    """value 越大，≤ value 占比不减（rank 单调不减，验收点）。"""
    sample = [1, 3, 3, 5, 8, 13]
    prev = -1.0
    for v in range(0, 15):
        r = _percentile_rank(sample, v)
        assert r is not None
        assert r >= prev, f"rank 在 value={v} 处下降：{r} < {prev}"
        prev = r
    # 边界：最大值处 rank=1.0
    assert _percentile_rank(sample, 13) == 1.0
    # 比最小值还小：rank=0
    assert _percentile_rank(sample, 0) == 0.0


def test_peer_sample_cutoff_prevents_lookahead():
    """cutoff：只有 end_bar_dt ≤ 当前 bar_dt 的样本入分布（防前视，L9）。"""
    s = _PeerSample()
    s.add(_WaveFinalStat(Direction.UP, _D0 + timedelta(days=10), 3, 1))
    s.add(_WaveFinalStat(Direction.UP, _D0 + timedelta(days=20), 5, 2))
    s.add(_WaveFinalStat(Direction.UP, _D0 + timedelta(days=30), 7, 4))
    s.finalize()
    # cutoff=第15天：只有第10天那条入样本
    r = s.update_rank(Direction.UP, 3, _D0 + timedelta(days=15))
    assert r == 1.0  # 样本只有 [3]，value=3 → 100%
    # cutoff=第25天：[3,5] 入样本，value=3 → 50%
    r2 = s.update_rank(Direction.UP, 3, _D0 + timedelta(days=25))
    assert r2 == 0.5
    # 反方向无样本 → None
    assert s.update_rank(Direction.DOWN, 3, _D0 + timedelta(days=30)) is None


# =========================================================================
# 计数：new_count / no_new_span（端到端对账）
# =========================================================================
def test_uninitialized_positions_all_zero():
    """结构未成立：每 bar 计数 0、无 wave 链路、rank=None。"""
    seq = [(10.0, 9.0), (10.5, 9.5), (10.2, 9.2)]  # 太短，不成 wave
    bars, result, positions = _positions(seq)
    assert len(positions) == len(bars)
    for p in positions:
        assert p.system_state == SystemState.UNINITIALIZED
        assert p.wave_id is None
        assert p.new_count == 0
        assert p.no_new_span == 0
        assert p.update_rank is None
        assert p.life_state is None


def test_initial_wave_new_count_and_no_new_span():
    """initial up wave 确认 bar：new_count=1, no_new_span=0；之后 alive 无推进则 no_new_span 累加。"""
    seq = [
        (10.0, 9.0),   # 0
        (12.0, 11.0),  # 1 H0=12
        (11.5, 10.0),  # 2
        (11.0, 8.0),   # 3 L1=8 (guard)
        (11.8, 9.5),   # 4
        (14.0, 12.5),  # 5 H2=14 -> up_alive 确认 bar
        (13.0, 12.0),  # 6 alive 无推进
        (13.2, 12.2),  # 7 alive 无推进
    ]
    bars, result, positions = _positions(seq)
    assert positions[-1].system_state == SystemState.UP_ALIVE
    # 找到 wave 确认 bar（第一条 up_alive）
    confirm_idx = next(i for i, p in enumerate(positions) if p.system_state == SystemState.UP_ALIVE)
    assert positions[confirm_idx].new_count == 1
    assert positions[confirm_idx].no_new_span == 0
    assert positions[confirm_idx].direction == Direction.UP
    # 确认 bar 之后无推进的 bar：no_new_span 单调累加，new_count 不变
    after = positions[confirm_idx + 1:]
    prev_span = positions[confirm_idx].no_new_span
    for p in after:
        if p.system_state == SystemState.UP_ALIVE and p.wave_id == positions[confirm_idx].wave_id:
            assert p.new_count == 1  # 本序列确认后无新 HH
            assert p.no_new_span >= prev_span
            prev_span = p.no_new_span


# =========================================================================
# transition：direction = old_direction（L-T5）+ transition_span 不并入新波
# =========================================================================
def test_transition_keeps_old_direction_and_span_isolated():
    """break 后 transition 期 direction=old_direction；transition_span 独立于新波 no_new_span。"""
    seq = [
        (10.0, 9.0),
        (12.0, 11.0),  # H0=12
        (11.5, 10.0),
        (11.0, 8.0),   # L1=8 guard
        (11.8, 9.5),
        (14.0, 12.5),  # H2=14 up_alive
        (13.0, 12.0),
        (12.0, 7.99),  # 跌破 guard → break → transition
        (11.0, 10.5),
        (11.5, 10.8),
    ]
    bars, result, positions = _positions(seq)
    assert len(result.breaks) == 1
    trans_positions = [p for p in positions if p.system_state == SystemState.TRANSITION]
    assert trans_positions, "应存在 transition 期 position"
    for p in trans_positions:
        # L-T5：transition 期保留 old_direction（本例 up wave break → old_direction=up）
        assert p.direction == Direction.UP
        assert p.transition_span >= 1
        assert p.old_wave_id is not None


# =========================================================================
# birth_type：initial / opposite
# =========================================================================
def test_birth_type_initial_for_first_wave():
    """首个 wave 无产生它的 transition → birth_type=initial，confirmation_distance=None。"""
    seq = [
        (10.0, 9.0),
        (12.0, 11.0),
        (11.5, 10.0),
        (11.0, 8.0),
        (11.8, 9.5),
        (14.0, 12.5),  # up_alive 确认
        (13.0, 12.0),
    ]
    bars, result, positions = _positions(seq)
    alive = [p for p in positions if p.system_state == SystemState.UP_ALIVE]
    assert alive
    for p in alive:
        assert p.birth_type == BirthType.INITIAL
        assert p.confirmation_distance_abs is None


# =========================================================================
# life_state：terminated → terminal 优先
# =========================================================================
def test_life_state_terminal_when_wave_core_terminated():
    """构造一条带 terminated 计数的 position，断言 life_state=terminal（优先级最高）。"""
    from asteria.malf.lifespan import _life_state
    from asteria.malf.types import WavePosition

    pos = WavePosition(
        symbol="TEST",
        timeframe="day",
        bar_dt=_D0,
        wave_core_state=WaveCoreState.TERMINATED,
        update_rank=0.5,
        stagnation_rank=0.1,  # 即使不停滞，terminated 也优先 terminal
    )
    assert _life_state(pos, LifespanConfig()) == LifeState.TERMINAL


def test_life_state_order_stagnant_over_extended():
    """stagnant（高 stagnation_rank）优先于 extended（高 update_rank）。"""
    from asteria.malf.lifespan import _life_state
    from asteria.malf.types import WavePosition

    pos = WavePosition(
        symbol="TEST",
        timeframe="day",
        bar_dt=_D0,
        wave_core_state=WaveCoreState.ALIVE,
        update_rank=0.95,      # 高 update → 本应 extended
        stagnation_rank=0.95,  # 但高 stagnation 优先 → stagnant
    )
    assert _life_state(pos, LifespanConfig()) == LifeState.STAGNANT


def test_positions_one_per_bar():
    """每 bar 恰好一条 WavePosition（O7 逐 bar 发布）。"""
    seq = [(10.0 + i * 0.1, 9.0 + i * 0.1) for i in range(12)]
    bars, result, positions = _positions(seq)
    assert len(positions) == len(bars)
    assert [p.bar_dt for p in positions] == [b.bar_dt for b in bars]


# =========================================================================
# P3：break bar 真实产出 life_state=terminal（真实 Core 输出路径，非手工构造）
# =========================================================================
def test_terminal_life_state_emitted_on_real_break_bar():
    """break bar 应真实产出 wave_core_state=terminated → life_state=terminal。

    回归 P3：Core break 后 active_wave=None、snap.wave_core_state=None，
    lifespan 据 break_event 在该 bar 标记 terminated，否则 life_state=terminal
    永不出现，使 PAS 的 terminal 条件成为死分支。
    """
    seq = [
        (10.0, 9.0),
        (12.0, 11.0),  # H0=12
        (11.5, 10.0),
        (11.0, 8.0),   # L1=8 guard
        (11.8, 9.5),
        (14.0, 12.5),  # H2=14 up_alive
        (13.0, 12.0),
        (12.0, 7.99),  # 跌破 guard → break bar
        (11.0, 10.5),
        (11.5, 10.8),
    ]
    bars, result, positions = _positions(seq)
    assert len(result.breaks) == 1
    break_dt = result.breaks[0].break_bar_dt
    break_pos = next(p for p in positions if p.bar_dt == break_dt)
    # break bar 上旧波终止 → terminal（direction 保留 old_direction，L-T5）
    assert break_pos.wave_core_state == WaveCoreState.TERMINATED
    assert break_pos.life_state == LifeState.TERMINAL
    assert break_pos.direction == Direction.UP
    # 恰好产出至少一条 terminal（不是死分支）
    terminal_positions = [p for p in positions if p.life_state == LifeState.TERMINAL]
    assert len(terminal_positions) >= 1


# =========================================================================
# P1：birth confirmation_distance 在确认 bar 算一次，不被后续推进改写
# =========================================================================
def test_birth_confirmation_distance_stable_across_wave():
    """同一条 wave 的 birth descriptor 在所有 bar 上必须一致（不随后续 HH/LL 改写）。

    回归 P1：构造 break→反向新波→新波后续推进多次的序列，断言新波每根 bar 的
    confirmation_distance_abs 恒等于确认 bar 的值（L-T7：birth 描述形成过程）。
    """
    # up wave → break → 反向 down wave 确认 → down wave 多次推进新 LL（new_count 1→4）。
    # 关键：wave 2 确认后 progress_extreme 不断被更低 LL 替换。若 birth 每 bar 重算，
    # confirmation_distance 会从「确认 pivot vs boundary」被改写成「后续更低 LL vs boundary」。
    seq = [
        (10.0, 9.0),    # 0
        (12.0, 11.0),   # 1 H0=12
        (11.5, 10.0),   # 2
        (11.0, 8.0),    # 3 L1=8 guard
        (11.8, 9.5),    # 4
        (14.0, 12.5),   # 5 H2=14 up_alive（HH=14, guard=8）
        (13.0, 12.0),   # 6
        (13.5, 7.5),    # 7 break guard 8.0 → transition（boundary_low=8）
        (12.0, 9.0),    # 8
        (13.0, 9.5),    # 9 候选 H=13
        (10.0, 5.0),    # 10 L=5 < boundary_low 8 → 确认 down wave（confirmation pivot L=5）
        (9.0, 6.5),     # 11 反弹（确认 L=5 为 pivot）
        (8.0, 3.0),     # 12 更低 LL=3 → progress 推进（new_count 2）
        (7.0, 4.5),     # 13 反弹（确认 L=3）
        (6.0, 2.0),     # 14 更低 LL=2 → 推进
        (5.5, 3.5),     # 15 反弹
    ]
    bars, result, positions = _positions(seq)

    # 前置断言：wave 2 确实形成且 new_count 真的推进过（否则测试空过，无判别力）
    w2_positions = [p for p in positions if p.wave_id == 2]
    assert w2_positions, "未形成由 transition 确认的第二条 wave，测试无判别力"
    assert max(p.new_count for p in w2_positions) >= 3, "wave 2 未发生多次推进，无法证明距离稳定性"
    # wave 2 必须有 confirmation_distance（由 transition 确认 → birth 距离非空）
    w2_distances = {p.confirmation_distance_abs for p in w2_positions if p.confirmation_distance_abs is not None}
    assert w2_distances, "wave 2 无 confirmation_distance，测试空过"

    # 核心断言：尽管 new_count 从 1 推进到 ≥3，confirmation_distance 跨所有 bar 恒为唯一值
    assert len(w2_distances) == 1, (
        f"wave 2 的 confirmation_distance 被后续推进改写：{w2_distances}（P1 回归失败）"
    )
