"""PAS Core 确定性测试（M3 验证）。

测试覆盖：
- Posture Matrix 全枚举（5×5 = 25 种 Premise × ReadStatus 组合）
- PM-T6 降档规则
- C6 上限约束
- 内部三态树逻辑
"""

from datetime import date

import pytest

from asteria.malf.types import (
    BoundaryPressureRegime,
    ContinuationRegime,
    Direction,
    LifeState,
    StagnationRegime,
    SystemState,
    WaveBehaviorSnapshot,
    WaveCoreState,
    WavePosition,
)
from asteria.pas.core import _posture_matrix, derive_pas_snapshots
from asteria.pas.types import DirectionalPremise, Posture, ReadStatus


# =========================================================================
# 辅助函数：构造测试数据
# =========================================================================
def make_position(
    system_state: SystemState = SystemState.UP_ALIVE,
    wave_core_state: WaveCoreState = WaveCoreState.ALIVE,
    direction: Direction = Direction.UP,
    new_count: int = 2,
    no_new_span: int = 1,
    life_state: LifeState = LifeState.EARLY,
    wave_id: int = 1,
) -> WavePosition:
    """构造 WavePosition 测试数据。"""
    return WavePosition(
        symbol="600000.SH",
        timeframe="day",
        bar_dt=date(2025, 1, 10),
        wave_id=wave_id,
        system_state=system_state,
        wave_core_state=wave_core_state,
        direction=direction,
        new_count=new_count,
        no_new_span=no_new_span,
        life_state=life_state,
    )


def make_behavior(
    continuation: ContinuationRegime = ContinuationRegime.ADVANCING,
    stagnation: StagnationRegime = StagnationRegime.FRESH,
    boundary: BoundaryPressureRegime = BoundaryPressureRegime.CONTINUATION_SIDE,
) -> WaveBehaviorSnapshot:
    """构造 WaveBehaviorSnapshot 测试数据。"""
    return WaveBehaviorSnapshot(
        symbol="600000.SH",
        timeframe="day",
        bar_dt=date(2025, 1, 10),
        wave_id=1,
        direction=Direction.UP,
        continuation_regime=continuation,
        stagnation_regime=stagnation,
        boundary_pressure_regime=boundary,
    )


# =========================================================================
# 测试 1：内部三态树逻辑
# =========================================================================
def test_tri_state_denying():
    """Denying 条件：transition / guard broken / terminal / transitioning / terminal_pressure。"""
    # transition
    pos = make_position(system_state=SystemState.TRANSITION, no_new_span=1)
    beh = make_behavior()
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].directional_premise == DirectionalPremise.EXPECT_WEAKNESS_REJECTION


def test_tri_state_proving():
    """Proving 条件：8 条全成立。"""
    pos = make_position(
        system_state=SystemState.UP_ALIVE,
        new_count=1,
        no_new_span=2,
        life_state=LifeState.EARLY,
    )
    beh = make_behavior(
        continuation=ContinuationRegime.ADVANCING,
        stagnation=StagnationRegime.FRESH,
        boundary=BoundaryPressureRegime.CONTINUATION_SIDE,
    )
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].directional_premise == DirectionalPremise.EXPECT_STRENGTH_CONTINUATION


def test_tri_state_neutral_terminal_observation():
    """Neutral 1: no_new_span >= 20 → no_actionable_premise。"""
    pos = make_position(no_new_span=20)
    beh = make_behavior()
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].directional_premise == DirectionalPremise.NO_ACTIONABLE_PREMISE


def test_tri_state_neutral_stagnant():
    """Neutral 2: no_new_span >= 10 或 stalled → expect_boundary_test。"""
    pos = make_position(no_new_span=10)
    beh = make_behavior()
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].directional_premise == DirectionalPremise.EXPECT_BOUNDARY_TEST


def test_tri_state_neutral_slowing():
    """Neutral 3: continuation = slowing → expect_boundary_test。"""
    pos = make_position(no_new_span=3)
    beh = make_behavior(continuation=ContinuationRegime.SLOWING)
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].directional_premise == DirectionalPremise.EXPECT_BOUNDARY_TEST


def test_tri_state_neutral_newborn():
    """Neutral 4: new_count = 0 → no_actionable_premise。"""
    pos = make_position(new_count=0)
    beh = make_behavior()
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].directional_premise == DirectionalPremise.NO_ACTIONABLE_PREMISE


def test_tri_state_neutral_watchful():
    """Neutral 5: 其余 → expect_transition_resolution。"""
    pos = make_position(new_count=1, no_new_span=6)
    beh = make_behavior(continuation=ContinuationRegime.ADVANCING)
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].directional_premise == DirectionalPremise.EXPECT_TRANSITION_RESOLUTION


# =========================================================================
# 测试 2：Posture Matrix PM-T1~T5（精确匹配）
# =========================================================================
def test_pm_t1_strength_continuation_strong():
    """PM-T1: strength_continuation + strong → (allowed, blocked, favored, favored, deferred)。"""
    # 构造 Proving 条件 → strength_continuation
    pos = make_position(
        system_state=SystemState.UP_ALIVE,
        new_count=1,
        no_new_span=2,
        life_state=LifeState.EARLY,
    )
    # 构造 strong evidence：3 strength, 0 weakness
    beh = make_behavior(
        continuation=ContinuationRegime.ADVANCING,
        stagnation=StagnationRegime.FRESH,
        boundary=BoundaryPressureRegime.CONTINUATION_SIDE,
    )
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].directional_premise == DirectionalPremise.EXPECT_STRENGTH_CONTINUATION
    assert snaps[0].read_status == ReadStatus.STRONG
    assert snaps[0].tst_posture == Posture.ALLOWED
    assert snaps[0].bof_posture == Posture.BLOCKED
    assert snaps[0].bpb_posture == Posture.FAVORED
    assert snaps[0].pb_posture == Posture.FAVORED
    assert snaps[0].cpb_posture == Posture.DEFERRED


def test_pm_t2_weakness_rejection_weak():
    """PM-T2: weakness_rejection + weak → (allowed, favored, blocked, blocked, deferred)。"""
    # 构造 Denying 条件 → weakness_rejection。
    # 🔒 用 life_state=TERMINAL 触发 Denying（非 transition），避免 C6 transition_bound 压档。
    pos = make_position(system_state=SystemState.UP_ALIVE, life_state=LifeState.TERMINAL)
    # 构造 weak evidence：0 strength, 3 weakness
    beh = make_behavior(
        continuation=ContinuationRegime.STALLED,
        stagnation=StagnationRegime.TERMINAL_PRESSURE,
        boundary=BoundaryPressureRegime.GUARD_PRESSURE,
    )
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].directional_premise == DirectionalPremise.EXPECT_WEAKNESS_REJECTION
    assert snaps[0].read_status == ReadStatus.WEAK
    assert snaps[0].tst_posture == Posture.ALLOWED
    assert snaps[0].bof_posture == Posture.FAVORED
    assert snaps[0].bpb_posture == Posture.BLOCKED
    assert snaps[0].pb_posture == Posture.BLOCKED
    assert snaps[0].cpb_posture == Posture.DEFERRED


def test_pm_t3_boundary_test_mixed():
    """PM-T3: boundary_test + mixed → (favored, allowed, deferred, deferred, deferred)。"""
    # 构造 Neutral stagnant → boundary_test（no_new_span>=10）
    pos = make_position(no_new_span=10)
    # 构造 mixed evidence：1 strength + 1 weakness + 1 ambiguity（strength==weakness 且未被 ambiguity 主导）
    beh = make_behavior(
        continuation=ContinuationRegime.ADVANCING,  # strength
        stagnation=StagnationRegime.STALLED,  # weakness
        boundary=BoundaryPressureRegime.NEUTRAL,  # ambiguity
    )
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].directional_premise == DirectionalPremise.EXPECT_BOUNDARY_TEST
    assert snaps[0].read_status == ReadStatus.MIXED
    assert snaps[0].tst_posture == Posture.FAVORED
    assert snaps[0].bof_posture == Posture.ALLOWED
    assert snaps[0].bpb_posture == Posture.DEFERRED
    assert snaps[0].pb_posture == Posture.DEFERRED
    assert snaps[0].cpb_posture == Posture.DEFERRED


def test_pm_t4_transition_resolution_ambiguous():
    """PM-T4: transition_resolution + ambiguous → (deferred, deferred, blocked, blocked, blocked)。"""
    # 构造 Neutral watchful → transition_resolution
    # （非 slowing/stagnant/newborn/terminal：continuation≠slowing, no_new_span∈[5,10), new_count≥1）
    pos = make_position(new_count=1, no_new_span=6)
    # ambiguous evidence：1 strength + 2 ambiguity（ambiguity 主导）
    beh = make_behavior(
        continuation=ContinuationRegime.ADVANCING,  # strength（slowing 会落入 subtype-3）
        stagnation=StagnationRegime.WATCHFUL,  # ambiguity
        boundary=BoundaryPressureRegime.NEUTRAL,  # ambiguity
    )
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].directional_premise == DirectionalPremise.EXPECT_TRANSITION_RESOLUTION
    assert snaps[0].read_status == ReadStatus.AMBIGUOUS
    assert snaps[0].tst_posture == Posture.DEFERRED
    assert snaps[0].bof_posture == Posture.DEFERRED
    assert snaps[0].bpb_posture == Posture.BLOCKED
    assert snaps[0].pb_posture == Posture.BLOCKED
    assert snaps[0].cpb_posture == Posture.BLOCKED


def test_pm_t5_no_actionable_premise():
    """PM-T5: no_actionable_premise → 全 blocked。"""
    # 构造 Neutral terminal_observation → no_actionable_premise
    pos = make_position(no_new_span=20)
    beh = make_behavior()
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].directional_premise == DirectionalPremise.NO_ACTIONABLE_PREMISE
    assert snaps[0].tst_posture == Posture.BLOCKED
    assert snaps[0].bof_posture == Posture.BLOCKED
    assert snaps[0].bpb_posture == Posture.BLOCKED
    assert snaps[0].pb_posture == Posture.BLOCKED
    assert snaps[0].cpb_posture == Posture.BLOCKED


# =========================================================================
# 测试 3：PM-T6 降档规则（ReadStatus 与 Premise 不匹配）
# =========================================================================
def test_pm_t6_downgrade_mismatch():
    """PM-T6：ReadStatus 与 Premise 不匹配 → 全体降一档。

    注意：strength_continuation+weak 不可达（Proving 的 8 条约束必然产出 strong evidence）。
    取可达的 mismatch：boundary_test（stagnant 子类，no_new_span≥10）+ strong（3 条 strength regime）。
    no_new_span≥10 使 Proving 失败（要求 <5），故落 Neutral→stagnant→boundary_test；
    但 behavior 三条全 strength → read=strong，与 boundary_test 期望的 mixed 不匹配 → PM-T6。
    """
    pos = make_position(no_new_span=10)  # → Neutral stagnant → boundary_test
    beh = make_behavior(
        continuation=ContinuationRegime.ADVANCING,  # strength
        stagnation=StagnationRegime.FRESH,  # strength
        boundary=BoundaryPressureRegime.CONTINUATION_SIDE,  # strength
    )
    snaps = derive_pas_snapshots([pos], [beh])
    # boundary_test + strong（≠mixed）→ PM-T6 降档
    # base(favored, allowed, deferred, deferred, deferred) → 降档 (allowed, deferred, blocked, blocked, blocked)
    assert snaps[0].directional_premise == DirectionalPremise.EXPECT_BOUNDARY_TEST
    assert snaps[0].read_status == ReadStatus.STRONG
    assert snaps[0].tst_posture == Posture.ALLOWED
    assert snaps[0].bof_posture == Posture.DEFERRED
    assert snaps[0].bpb_posture == Posture.BLOCKED
    assert snaps[0].pb_posture == Posture.BLOCKED
    assert snaps[0].cpb_posture == Posture.BLOCKED


# =========================================================================
# 测试 4：C6 上限约束
# =========================================================================
def test_c6_transition_bound_cap_deferred():
    """C6: transition_bound → 上限 deferred。"""
    pos = make_position(system_state=SystemState.TRANSITION)
    beh = make_behavior()
    snaps = derive_pas_snapshots([pos], [beh])
    # transition → weakness_rejection + weak → PM-T2 → (allowed, favored, blocked, blocked, deferred)
    # transition_bound → cap deferred → (deferred, deferred, blocked, blocked, deferred)
    assert snaps[0].transition_bound is True
    assert snaps[0].tst_posture == Posture.DEFERRED
    assert snaps[0].bof_posture == Posture.DEFERRED


def test_c6_lineage_gap_all_blocked():
    """C6: lineage_gap → 全 blocked。"""
    pos = make_position(wave_id=None)  # 无 wave_id
    beh = make_behavior()
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].lineage_gap is True
    assert snaps[0].tst_posture == Posture.BLOCKED
    assert snaps[0].bof_posture == Posture.BLOCKED
    assert snaps[0].bpb_posture == Posture.BLOCKED
    assert snaps[0].pb_posture == Posture.BLOCKED
    assert snaps[0].cpb_posture == Posture.BLOCKED


def test_c6_ambiguity_dominates_cap_deferred():
    """C6: ambiguity 主导 → 上限 deferred。"""
    # 构造 ambiguity > (strength + weakness)
    pos = make_position(new_count=1, no_new_span=6)
    beh = make_behavior(
        continuation=ContinuationRegime.SLOWING,  # ambiguity
        stagnation=StagnationRegime.WATCHFUL,  # ambiguity
        boundary=BoundaryPressureRegime.NEUTRAL,  # ambiguity
    )
    snaps = derive_pas_snapshots([pos], [beh])
    # ambiguity=3, strength=0, weakness=0 → ambiguity_dominates
    assert snaps[0].ambiguity_dominates is True
    # transition_resolution + ambiguous → PM-T4 → (deferred, deferred, blocked, blocked, blocked)
    # cap deferred 对已是 deferred/blocked 的不变
    assert snaps[0].tst_posture == Posture.DEFERRED
    assert snaps[0].bof_posture == Posture.DEFERRED


# =========================================================================
# 测试 5：EvidenceTriplet 收集
# =========================================================================
def test_evidence_collection():
    """验证 EvidenceTriplet 计数正确。"""
    pos = make_position()
    beh = make_behavior(
        continuation=ContinuationRegime.ADVANCING,  # strength
        stagnation=StagnationRegime.FRESH,  # strength
        boundary=BoundaryPressureRegime.CONTINUATION_SIDE,  # strength
    )
    snaps = derive_pas_snapshots([pos], [beh])
    assert snaps[0].strength_evidence_count == 3
    assert snaps[0].weakness_evidence_count == 0
    assert snaps[0].ambiguity_evidence_count == 0


# =========================================================================
# 测试 6：批量处理
# =========================================================================
def test_batch_processing():
    """批量处理多 bar，验证逐 bar 对齐。"""
    positions = [
        make_position(new_count=1, no_new_span=i) for i in range(3)
    ]
    behaviors = [make_behavior() for _ in range(3)]
    # 修改 bar_dt 使其唯一
    for i, (p, b) in enumerate(zip(positions, behaviors)):
        p.bar_dt = date(2025, 1, 10 + i)
        b.bar_dt = date(2025, 1, 10 + i)

    snaps = derive_pas_snapshots(positions, behaviors)
    assert len(snaps) == 3
    for i, snap in enumerate(snaps):
        assert snap.bar_dt == date(2025, 1, 10 + i)


# =========================================================================
# 测试 7：input contract 校验
# =========================================================================
def test_input_contract_validation():
    """验证 input contract 不对齐时抛错。"""
    pos = make_position()
    beh = make_behavior()
    beh.symbol = "000001.SZ"  # 不匹配
    with pytest.raises(ValueError, match="不对齐"):
        derive_pas_snapshots([pos], [beh])


# =========================================================================
# 测试 8：Posture Matrix 5×5 全枚举确定性（REBUILD_PLAN §12 验收口径）
# =========================================================================
# 直接测纯查表函数 _posture_matrix：它独立于三态树可达性，必须对全部
# 25 格 (DirectionalPremise × ReadStatus) 组合确定。很多组合在三态树
# 流水线上不可达（如 strength_continuation+weak），但查表函数本身仍须有
# 确定结果——这正是 PM-T4「Posture Matrix Is Deterministic」要求的层级。
#
# Oracle 用 4 档单字母简写显式列出 25 行（手算自规范 PM-T1~T6），
# 不靠循环重算实现逻辑，避免测试与被测同源。
F = Posture.FAVORED
A = Posture.ALLOWED
D = Posture.DEFERRED
B = Posture.BLOCKED

_SC = DirectionalPremise.EXPECT_STRENGTH_CONTINUATION
_WR = DirectionalPremise.EXPECT_WEAKNESS_REJECTION
_BT = DirectionalPremise.EXPECT_BOUNDARY_TEST
_TR = DirectionalPremise.EXPECT_TRANSITION_RESOLUTION
_NA = DirectionalPremise.NO_ACTIONABLE_PREMISE

_RS = ReadStatus.STRONG
_RW = ReadStatus.WEAK
_RM = ReadStatus.MIXED
_RA = ReadStatus.AMBIGUOUS
_RN = ReadStatus.NOT_APPLICABLE

# (premise, read_status) -> 期望 (TST, BOF, BPB, PB, CPB)
# 命中规则标在行尾注释。
POSTURE_MATRIX_ORACLE = {
    # --- expect_strength_continuation (base TST,BOF,BPB,PB,CPB = A,B,F,F,D) ---
    (_SC, _RS): (A, B, F, F, D),  # PM-T1 命中
    (_SC, _RW): (D, B, A, A, B),  # PM-T6 降档一次
    (_SC, _RM): (D, B, A, A, B),  # PM-T6
    (_SC, _RA): (D, B, A, A, B),  # PM-T6
    (_SC, _RN): (B, B, B, B, B),  # PM-T5 (read=not_applicable)
    # --- expect_weakness_rejection (base A,F,B,B,D) ---
    (_WR, _RS): (D, A, B, B, B),  # PM-T6
    (_WR, _RW): (A, F, B, B, D),  # PM-T2 命中
    (_WR, _RM): (D, A, B, B, B),  # PM-T6
    (_WR, _RA): (D, A, B, B, B),  # PM-T6
    (_WR, _RN): (B, B, B, B, B),  # PM-T5
    # --- expect_boundary_test (base F,A,D,D,D) ---
    (_BT, _RS): (A, D, B, B, B),  # PM-T6
    (_BT, _RW): (A, D, B, B, B),  # PM-T6
    (_BT, _RM): (F, A, D, D, D),  # PM-T3 命中
    (_BT, _RA): (A, D, B, B, B),  # PM-T6
    (_BT, _RN): (B, B, B, B, B),  # PM-T5
    # --- expect_transition_resolution (base D,D,B,B,B) ---
    (_TR, _RS): (B, B, B, B, B),  # PM-T6 降档（D→B, B→B）
    (_TR, _RW): (B, B, B, B, B),  # PM-T6
    (_TR, _RM): (B, B, B, B, B),  # PM-T6
    (_TR, _RA): (D, D, B, B, B),  # PM-T4 命中
    (_TR, _RN): (B, B, B, B, B),  # PM-T5
    # --- no_actionable_premise → 全部 PM-T5（premise=no_actionable 优先）---
    (_NA, _RS): (B, B, B, B, B),
    (_NA, _RW): (B, B, B, B, B),
    (_NA, _RM): (B, B, B, B, B),
    (_NA, _RA): (B, B, B, B, B),
    (_NA, _RN): (B, B, B, B, B),
}


@pytest.mark.parametrize(("premise", "read_status"), list(POSTURE_MATRIX_ORACLE.keys()))
def test_posture_matrix_full_enumeration(premise, read_status):
    """5×5 全枚举：每格 (premise, read_status) 查表结果须与 oracle 完全一致。"""
    expected = POSTURE_MATRIX_ORACLE[(premise, read_status)]
    result = _posture_matrix(premise, read_status, [])
    assert result == expected, (
        f"{premise.value}+{read_status.value}: 期望 {[p.value for p in expected]}, "
        f"实得 {[p.value for p in result]}"
    )


def test_posture_matrix_oracle_is_complete():
    """确保 oracle 覆盖全部 25 格（5 premise × 5 read_status），无遗漏。"""
    assert len(POSTURE_MATRIX_ORACLE) == 25


def test_pm_t6_downgrade_is_single_step_only():
    """PM-T6 验证：所有 mismatch 组合都只降一档，绝不连降两档。

    逐格比对：mismatch 命中 PM-T6 的格子，其值必须等于「基础 posture
    恰好降一档」，而非降两档或不降。
    """
    base_by_premise = {
        _SC: (A, B, F, F, D),
        _WR: (A, F, B, B, D),
        _BT: (F, A, D, D, D),
        _TR: (D, D, B, B, B),
    }
    one_step = {F: A, A: D, D: B, B: B}
    two_step = {F: D, A: B, D: B, B: B}

    # 各 premise 的「匹配」read_status（命中 T1~T4，不走 T6）
    matched = {_SC: _RS, _WR: _RW, _BT: _RM, _TR: _RA}

    for premise, base in base_by_premise.items():
        for read_status in (_RS, _RW, _RM, _RA):
            if read_status == matched[premise]:
                continue  # 跳过匹配格（不走 T6）
            result = _posture_matrix(premise, read_status, [])
            expected_one = tuple(one_step[p] for p in base)
            expected_two = tuple(two_step[p] for p in base)
            assert result == expected_one, (
                f"{premise.value}+{read_status.value} 应降一档={expected_one}, 实得={result}"
            )
            # 显式反证：不得等于降两档（除非两者恰好相同，如全 B 行）
            if expected_one != expected_two:
                assert result != expected_two, (
                    f"{premise.value}+{read_status.value} 误降两档"
                )
