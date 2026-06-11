"""MALF v1.5 行为层测试：6 regime 派生确定性 / 比较铁律 / 越界禁止。

直接构造 WavePosition + CoreStateSnapshot 对照表，断言派生唯一确定（查表确定性）。
验收点（计划）：
- 6 regime 查表确定性：覆盖各 bucket。
- 比较铁律：transition 优先 continuation；无 guard 不出 guard_pressure；无 distance→unknown_birth。
- 越界禁止：快照对象不含 strength/setup/accept 等字段。
"""

from __future__ import annotations

import sys
from dataclasses import fields
from datetime import date, timedelta
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from asteria.malf.behavior import (  # noqa: E402
    BehaviorConfig,
    derive_behavior_snapshots,
)
from asteria.malf.types import (  # noqa: E402
    BirthQualityRegime,
    BirthType,
    BoundaryPressureRegime,
    ContinuationRegime,
    CoreStateSnapshot,
    Direction,
    DirectionalContinuityRegime,
    LifeState,
    StagnationRegime,
    SystemState,
    TransitionRegime,
    WaveBehaviorSnapshot,
    WaveCoreState,
    WavePosition,
)

_D0 = date(2020, 1, 1)
_CFG = BehaviorConfig()


def _snap(
    system_state: SystemState,
    *,
    guard_price: float | None = None,
    open_transition_id: int | None = None,
    bar_i: int = 0,
) -> CoreStateSnapshot:
    return CoreStateSnapshot(
        symbol="TEST",
        timeframe="day",
        bar_dt=_D0 + timedelta(days=bar_i),
        system_state=system_state,
        current_effective_guard_price=guard_price,
        open_transition_id=open_transition_id,
    )


def _pos(
    *,
    system_state: SystemState,
    wave_core_state: WaveCoreState | None = None,
    direction: Direction | None = Direction.UP,
    new_count: int = 1,
    no_new_span: int = 0,
    transition_span: int = 0,
    life_state: LifeState | None = None,
    birth_type: BirthType | None = None,
    candidate_replacement_count: int | None = None,
    confirmation_distance_abs: float | None = None,
    bar_i: int = 0,
) -> WavePosition:
    return WavePosition(
        symbol="TEST",
        timeframe="day",
        bar_dt=_D0 + timedelta(days=bar_i),
        wave_id=1,
        system_state=system_state,
        wave_core_state=wave_core_state,
        direction=direction,
        new_count=new_count,
        no_new_span=no_new_span,
        transition_span=transition_span,
        life_state=life_state,
        birth_type=birth_type,
        candidate_replacement_count=candidate_replacement_count,
        confirmation_distance_abs=confirmation_distance_abs,
    )


def _derive_one(pos: WavePosition, snap: CoreStateSnapshot) -> WaveBehaviorSnapshot:
    out = derive_behavior_snapshots([pos], [snap], cfg=_CFG)
    assert len(out) == 1
    return out[0]


# =========================================================================
# continuation_regime
# =========================================================================
def test_continuation_advancing():
    pos = _pos(system_state=SystemState.UP_ALIVE, no_new_span=0, new_count=2,
               wave_core_state=WaveCoreState.ALIVE)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).continuation_regime == ContinuationRegime.ADVANCING


def test_continuation_slowing():
    pos = _pos(system_state=SystemState.UP_ALIVE, no_new_span=4, new_count=2,
               wave_core_state=WaveCoreState.ALIVE)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).continuation_regime == ContinuationRegime.SLOWING


def test_continuation_stalled():
    pos = _pos(system_state=SystemState.UP_ALIVE, no_new_span=12, new_count=2,
               wave_core_state=WaveCoreState.ALIVE)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).continuation_regime == ContinuationRegime.STALLED


def test_continuation_transition_priority():
    """🔒 比较铁律：transition 优先于一切延续 bucket。"""
    pos = _pos(system_state=SystemState.TRANSITION, no_new_span=0, new_count=2,
               wave_core_state=None, transition_span=1)
    snap = _snap(SystemState.TRANSITION, open_transition_id=1)
    assert _derive_one(pos, snap).continuation_regime == ContinuationRegime.TRANSITIONING


# =========================================================================
# stagnation_regime
# =========================================================================
def test_stagnation_fresh():
    pos = _pos(system_state=SystemState.UP_ALIVE, no_new_span=1,
               wave_core_state=WaveCoreState.ALIVE)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).stagnation_regime == StagnationRegime.FRESH


def test_stagnation_terminal_pressure_when_terminated():
    pos = _pos(system_state=SystemState.UP_ALIVE, no_new_span=1,
               wave_core_state=WaveCoreState.TERMINATED)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).stagnation_regime == StagnationRegime.TERMINAL_PRESSURE


def test_stagnation_terminal_pressure_when_life_terminal():
    pos = _pos(system_state=SystemState.UP_ALIVE, no_new_span=1,
               wave_core_state=WaveCoreState.ALIVE, life_state=LifeState.TERMINAL)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).stagnation_regime == StagnationRegime.TERMINAL_PRESSURE


# =========================================================================
# boundary_pressure_regime（🔒 无 guard 不出 guard_pressure）
# =========================================================================
def test_boundary_neutral_when_no_guard():
    """🔒 无 current_effective_guard → 落 neutral，绝不输出 guard_pressure。"""
    pos = _pos(system_state=SystemState.UP_ALIVE, wave_core_state=WaveCoreState.ALIVE)
    snap = _snap(SystemState.UP_ALIVE, guard_price=None)
    out = _derive_one(pos, snap)
    assert out.boundary_pressure_regime == BoundaryPressureRegime.NEUTRAL
    assert out.boundary_pressure_regime != BoundaryPressureRegime.GUARD_PRESSURE


def test_boundary_continuation_side_when_guard_intact():
    pos = _pos(system_state=SystemState.UP_ALIVE, wave_core_state=WaveCoreState.ALIVE)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).boundary_pressure_regime == BoundaryPressureRegime.CONTINUATION_SIDE


def test_boundary_transition_pressure():
    pos = _pos(system_state=SystemState.TRANSITION, wave_core_state=None, transition_span=1)
    snap = _snap(SystemState.TRANSITION, open_transition_id=1)
    assert _derive_one(pos, snap).boundary_pressure_regime == BoundaryPressureRegime.TRANSITION_PRESSURE


# =========================================================================
# directional_continuity_regime
# =========================================================================
def test_directional_same_for_initial():
    pos = _pos(system_state=SystemState.UP_ALIVE, wave_core_state=WaveCoreState.ALIVE,
               birth_type=BirthType.INITIAL)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).directional_continuity_regime == \
        DirectionalContinuityRegime.SAME_DIRECTION_CONTINUATION


def test_directional_opposite_rebirth():
    pos = _pos(system_state=SystemState.UP_ALIVE, wave_core_state=WaveCoreState.ALIVE,
               birth_type=BirthType.OPPOSITE_DIRECTION_AFTER_BREAK)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).directional_continuity_regime == \
        DirectionalContinuityRegime.OPPOSITE_DIRECTION_REBIRTH


def test_directional_transition_unresolved():
    pos = _pos(system_state=SystemState.TRANSITION, wave_core_state=None, transition_span=1)
    snap = _snap(SystemState.TRANSITION, open_transition_id=1)
    assert _derive_one(pos, snap).directional_continuity_regime == \
        DirectionalContinuityRegime.TRANSITION_UNRESOLVED


# =========================================================================
# transition_regime
# =========================================================================
def test_transition_not_applicable_for_initial():
    pos = _pos(system_state=SystemState.UP_ALIVE, wave_core_state=WaveCoreState.ALIVE,
               candidate_replacement_count=None)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).transition_regime == TransitionRegime.NOT_APPLICABLE


def test_transition_clean_handoff_low_replacement():
    pos = _pos(system_state=SystemState.UP_ALIVE, wave_core_state=WaveCoreState.ALIVE,
               candidate_replacement_count=0)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).transition_regime == TransitionRegime.CLEAN_HANDOFF


def test_transition_replacement_heavy():
    pos = _pos(system_state=SystemState.UP_ALIVE, wave_core_state=WaveCoreState.ALIVE,
               candidate_replacement_count=3)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).transition_regime == TransitionRegime.REPLACEMENT_HEAVY


def test_transition_prolonged_unresolved_when_open():
    pos = _pos(system_state=SystemState.TRANSITION, wave_core_state=None, transition_span=6)
    snap = _snap(SystemState.TRANSITION, open_transition_id=1)
    assert _derive_one(pos, snap).transition_regime == TransitionRegime.PROLONGED_UNRESOLVED


# =========================================================================
# birth_quality_regime（🔒 无 distance → unknown_birth）
# =========================================================================
def test_birth_unknown_when_no_distance():
    """🔒 无 confirmation_distance_* → unknown_birth。"""
    pos = _pos(system_state=SystemState.UP_ALIVE, wave_core_state=WaveCoreState.ALIVE,
               confirmation_distance_abs=None, candidate_replacement_count=0)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).birth_quality_regime == BirthQualityRegime.UNKNOWN_BIRTH


def test_birth_clean_when_distance_and_no_replacement():
    pos = _pos(system_state=SystemState.UP_ALIVE, wave_core_state=WaveCoreState.ALIVE,
               confirmation_distance_abs=0.5, candidate_replacement_count=0)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).birth_quality_regime == BirthQualityRegime.CLEAN_BIRTH


def test_birth_negotiated_one_replacement():
    pos = _pos(system_state=SystemState.UP_ALIVE, wave_core_state=WaveCoreState.ALIVE,
               confirmation_distance_abs=0.5, candidate_replacement_count=1)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).birth_quality_regime == BirthQualityRegime.NEGOTIATED_BIRTH


def test_birth_costly_many_replacements():
    pos = _pos(system_state=SystemState.UP_ALIVE, wave_core_state=WaveCoreState.ALIVE,
               confirmation_distance_abs=0.5, candidate_replacement_count=3)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    assert _derive_one(pos, snap).birth_quality_regime == BirthQualityRegime.COSTLY_BIRTH


# =========================================================================
# 长度不一致 → 报错；越界禁止字段
# =========================================================================
def test_length_mismatch_raises():
    pos = _pos(system_state=SystemState.UP_ALIVE, wave_core_state=WaveCoreState.ALIVE)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    try:
        derive_behavior_snapshots([pos, pos], [snap], cfg=_CFG)
        raised = False
    except ValueError:
        raised = True
    assert raised, "长度不一致应抛 ValueError"


def test_no_forbidden_fields_in_snapshot():
    """🔒 越界禁止：行为快照字段集不含强弱分/setup/accept/order/profit。"""
    field_names = {f.name for f in fields(WaveBehaviorSnapshot)}
    forbidden = {
        "strength_score", "strength_bucket", "setup_family", "triggered",
        "accepted", "rejected", "order_intent", "profit",
        "accept", "reject", "buy", "sell", "position", "fill",
    }
    leaked = field_names & forbidden
    assert not leaked, f"行为快照泄漏禁止字段：{leaked}"


def test_determinism_same_input_same_output():
    """C-T4 风格：相同输入必产相同 regime 组合。"""
    pos = _pos(system_state=SystemState.UP_ALIVE, no_new_span=3, new_count=2,
               wave_core_state=WaveCoreState.ALIVE, birth_type=BirthType.INITIAL)
    snap = _snap(SystemState.UP_ALIVE, guard_price=10.0)
    a = _derive_one(pos, snap)
    b = _derive_one(pos, snap)
    assert a.continuation_regime == b.continuation_regime
    assert a.stagnation_regime == b.stagnation_regime
    assert a.directional_continuity_regime == b.directional_continuity_regime
    assert a.boundary_pressure_regime == b.boundary_pressure_regime
    assert a.transition_regime == b.transition_regime
    assert a.birth_quality_regime == b.birth_quality_regime
