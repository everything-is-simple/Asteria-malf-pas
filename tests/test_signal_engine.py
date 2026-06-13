"""Signal 裁决引擎测试（质量门 D4 + 可变 RR D3）。

覆盖：reject 路径（family/posture/质量门/RR/tradable）+ accept 计划值（结构 T1/T2/可变 RR）
+ PAS 质量门「2+N」评级 + 可变 RR + to_feedback 不改输入 + 判定顺序。
"""

from datetime import date

from asteria.pas.types import (
    DirectionalPremise,
    PASCoreSnapshot,
    Posture,
    ReadStatus,
    SetupFamily,
)
from asteria.signal.engine import judge, to_feedback
from asteria.signal.types import (
    RejectReason,
    SignalConfig,
    SignalDecision,
)


def make_snap(
    *,
    tst: Posture | None = None,
    bof: Posture | None = None,
    bpb: Posture | None = None,
    pb: Posture | None = None,
    cpb: Posture | None = None,
    premise: DirectionalPremise = DirectionalPremise.EXPECT_STRENGTH_CONTINUATION,
    read: ReadStatus = ReadStatus.STRONG,
    strength: int = 2,
    weakness: int = 0,
    ambiguity: int = 0,
    transition_bound: bool = False,
    lineage_gap: bool = False,
    ambiguity_dominates: bool = False,
) -> PASCoreSnapshot:
    return PASCoreSnapshot(
        symbol="600000.SH",
        timeframe="day",
        bar_dt=date(2025, 1, 10),
        wave_id=1,
        directional_premise=premise,
        read_status=read,
        tst_posture=tst,
        bof_posture=bof,
        bpb_posture=bpb,
        pb_posture=pb,
        cpb_posture=cpb,
        strength_evidence_count=strength,
        weakness_evidence_count=weakness,
        ambiguity_evidence_count=ambiguity,
        transition_bound=transition_bound,
        lineage_gap=lineage_gap,
        ambiguity_dominates=ambiguity_dominates,
    )


# 一个让 RR 充分达标的结构前高：entry=10 stop=9.48 1R=0.52，前高 11.5 → RR≈3.88
_STRUCT = 11.5


# =========================================================================
# reject 路径
# =========================================================================
def test_reject_family_not_accepted():
    """family 不在 accept_families → FAMILY_NOT_ACCEPTED。"""
    snap = make_snap(tst=Posture.FAVORED)
    cfg = SignalConfig(accept_families=frozenset({SetupFamily.BOF}))
    cand = judge(snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.0, cfg=cfg)
    assert cand.decision == SignalDecision.REJECT
    assert cand.reason == RejectReason.FAMILY_NOT_ACCEPTED.value


def test_reject_posture_not_allowed():
    """该 family posture 不在 accepted_postures → POSTURE_NOT_ALLOWED。"""
    snap = make_snap(tst=Posture.BLOCKED)
    cand = judge(snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.0)
    assert cand.decision == SignalDecision.REJECT
    assert cand.reason == RejectReason.POSTURE_NOT_ALLOWED.value


def test_reject_posture_none():
    """该 family posture 为 None → POSTURE_NOT_ALLOWED。"""
    snap = make_snap(tst=None)
    cand = judge(snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.0)
    assert cand.decision == SignalDecision.REJECT
    assert cand.reason == RejectReason.POSTURE_NOT_ALLOWED.value


def test_reject_rr_below_min_no_structure():
    """无结构前高 → RR 退回 1.0 < 1.5 → RR_BELOW_MIN（惰性 RR 路径已消除）。"""
    snap = make_snap(tst=Posture.FAVORED)
    cand = judge(snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50)
    assert cand.decision == SignalDecision.REJECT
    assert cand.reason == RejectReason.RR_BELOW_MIN.value


def test_reject_rr_below_min_near_resistance():
    """前高离 entry 不足 1.5R → RR_BELOW_MIN（结构空间不够）。

    entry=10 stop=9.48 1R=0.52；前高 10.60 → RR=(10.60-10)/0.52≈1.15 < 1.5。
    """
    snap = make_snap(tst=Posture.FAVORED)
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=10.60,
    )
    assert cand.decision == SignalDecision.REJECT
    assert cand.reason == RejectReason.RR_BELOW_MIN.value


def test_reject_not_tradable():
    """tradable=False → NOT_TRADABLE（在 RR 之后）。需结构前高使 RR 先过。"""
    snap = make_snap(tst=Posture.FAVORED)
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=_STRUCT, tradable=False,
    )
    assert cand.decision == SignalDecision.REJECT
    assert cand.reason == RejectReason.NOT_TRADABLE.value


def test_reject_nonpositive_risk_unit():
    """entry ≤ stop（风险单位非正）→ RR_BELOW_MIN，不崩。"""
    snap = make_snap(tst=Posture.FAVORED)
    cand = judge(snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=11.0)
    assert cand.decision == SignalDecision.REJECT
    assert cand.reason == RejectReason.RR_BELOW_MIN.value


# =========================================================================
# PAS 质量门（D4「2+N」评级）
# =========================================================================
def test_quality_gate_read_status_too_weak():
    """read_status ∉ {strong,mixed} → READ_STATUS_TOO_WEAK（基本条件）。"""
    snap = make_snap(tst=Posture.FAVORED, read=ReadStatus.WEAK)
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=_STRUCT,
    )
    assert cand.decision == SignalDecision.REJECT
    assert cand.reason == RejectReason.READ_STATUS_TOO_WEAK.value


def test_quality_gate_top_grade_accepts():
    """顶配：favored+strong+continuation+证据偏强+无旗标 → score 5 → accept。"""
    snap = make_snap(
        tst=Posture.FAVORED, read=ReadStatus.STRONG,
        premise=DirectionalPremise.EXPECT_STRENGTH_CONTINUATION,
        strength=2, weakness=0,
    )
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=_STRUCT,
    )
    assert cand.decision == SignalDecision.ACCEPT


def test_quality_gate_marginal_passes_at_two():
    """边缘：allowed+mixed+可操作premise，证据平、无旗标 → score=2 → 默认门槛过。

    score: favored(0)+strong(0)+premise(1)+证据偏强(0,strength==weakness)+无旗标(1)=2。
    """
    snap = make_snap(
        bof=Posture.ALLOWED, read=ReadStatus.MIXED,
        premise=DirectionalPremise.EXPECT_BOUNDARY_TEST,
        strength=1, weakness=1,
    )
    cand = judge(
        snap, family=SetupFamily.BOF, expected_entry=10.0, t0_low=9.50,
        structural_target=_STRUCT,
    )
    assert cand.decision == SignalDecision.ACCEPT


def test_quality_gate_below_min_rejects():
    """allowed+mixed+不可操作premise，证据平、无旗标 → score=1 < 2 → QUALITY_BELOW_MIN。"""
    snap = make_snap(
        bof=Posture.ALLOWED, read=ReadStatus.MIXED,
        premise=DirectionalPremise.NO_ACTIONABLE_PREMISE,
        strength=1, weakness=1,
    )
    cand = judge(
        snap, family=SetupFamily.BOF, expected_entry=10.0, t0_low=9.50,
        structural_target=_STRUCT,
    )
    assert cand.decision == SignalDecision.REJECT
    assert cand.reason == RejectReason.QUALITY_BELOW_MIN.value


def test_quality_gate_veto_ambiguity_dominates():
    """veto_ambiguity_dominates=True 且 ambiguity_dominates → AMBIGUITY_DOMINATES。"""
    snap = make_snap(
        tst=Posture.FAVORED, read=ReadStatus.STRONG, ambiguity_dominates=True,
    )
    cfg = SignalConfig(veto_ambiguity_dominates=True)
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=_STRUCT, cfg=cfg,
    )
    assert cand.decision == SignalDecision.REJECT
    assert cand.reason == RejectReason.AMBIGUITY_DOMINATES.value


# =========================================================================
# life_state 上限（修复2：挡掉衰竭/末端的顶部 climax）
# =========================================================================
def test_quality_gate_life_state_exhausted_rejects():
    """life_state=terminal（衰竭）→ LIFE_STATE_EXHAUSTED，即使 posture/read/RR 全合格。"""
    snap = make_snap(tst=Posture.FAVORED, read=ReadStatus.STRONG)
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=_STRUCT, life_state="terminal",
    )
    assert cand.decision == SignalDecision.REJECT
    assert cand.reason == RejectReason.LIFE_STATE_EXHAUSTED.value


def test_quality_gate_life_state_extended_accepts():
    """life_state=extended（仍在白名单）→ 通过 life_state 上限。"""
    snap = make_snap(tst=Posture.FAVORED, read=ReadStatus.STRONG)
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=_STRUCT, life_state="extended",
    )
    assert cand.decision == SignalDecision.ACCEPT


def test_quality_gate_life_state_none_not_gated():
    """life_state=None（transition/样本不足）→ 不卡，留给 RR/posture 决定。"""
    snap = make_snap(tst=Posture.FAVORED, read=ReadStatus.STRONG)
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=_STRUCT, life_state=None,
    )
    assert cand.decision == SignalDecision.ACCEPT


def test_quality_gate_life_state_disabled_when_config_none():
    """accepted_life_states=None（关闭上限）→ 即使 terminal 也不卡。"""
    snap = make_snap(tst=Posture.FAVORED, read=ReadStatus.STRONG)
    cfg = SignalConfig(accepted_life_states=None)
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=_STRUCT, life_state="terminal", cfg=cfg,
    )
    assert cand.decision == SignalDecision.ACCEPT


# =========================================================================
# 可变 RR（D3：对结构前高算）
# =========================================================================
def test_variable_reward_risk_scales_with_structure():
    """同 posture，不同结构前高 → 不同 RR（不再恒等于 1）。"""
    snap = make_snap(tst=Posture.FAVORED)
    # entry=10 stop=9.48 1R=0.52
    near = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=11.0,  # RR=(11-10)/0.52≈1.92
    )
    far = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=12.0,  # RR=(12-10)/0.52≈3.85
    )
    assert near.decision == SignalDecision.ACCEPT
    assert far.decision == SignalDecision.ACCEPT
    assert far.reward_risk > near.reward_risk
    assert abs(near.reward_risk - (11.0 - 10.0) / 0.52) < 1e-6


# =========================================================================
# accept + 结构计划值
# =========================================================================
def test_accept_emits_structural_planned_values():
    """accept：stop=t0_low-0.02，结构 T1=min(前高,entry+1R)，T2=量度移动。

    entry=10.00 t0_low=9.50 → stop=9.48 1R=0.52；前高 11.06、guard 9.60。
    RR=(11.06-10)/0.52≈2.04≥1.5 → accept。
    target1=min(11.06,10.52)=10.52；target2=11.06+(11.06-9.60)=12.52。
    """
    snap = make_snap(tst=Posture.FAVORED)
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=11.06, structural_guard=9.60,
    )
    assert cand.decision == SignalDecision.ACCEPT
    assert cand.planned_stop == 9.48
    assert cand.planned_target1 == 10.52
    assert cand.planned_target2 == 12.52
    assert abs(cand.reward_risk - (11.06 - 10.0) / 0.52) < 1e-6
    assert cand.setup_family == SetupFamily.TST


def test_accept_allowed_posture():
    """allowed 也接受（默认 accepted_postures 含 favored+allowed），需结构前高使 RR 过。"""
    snap = make_snap(bof=Posture.ALLOWED)
    cand = judge(
        snap, family=SetupFamily.BOF, expected_entry=20.0, t0_low=19.0,
        structural_target=25.0,
    )
    assert cand.decision == SignalDecision.ACCEPT


def test_check_order_posture_before_quality():
    """判定顺序：posture 在质量门之前——posture 不合先 reject posture。"""
    snap = make_snap(tst=Posture.DEFERRED, read=ReadStatus.WEAK)
    cand = judge(snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.0)
    assert cand.reason == RejectReason.POSTURE_NOT_ALLOWED.value


def test_check_order_quality_before_rr():
    """判定顺序：质量门在 RR 之前——read 太弱即使 RR 会过也先报质量门。"""
    snap = make_snap(tst=Posture.FAVORED, read=ReadStatus.WEAK)
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=_STRUCT,
    )
    assert cand.reason == RejectReason.READ_STATUS_TOO_WEAK.value


def test_check_order_rr_before_tradable():
    """判定顺序：RR 在 tradable 之前——RR 不达标 + 不可交易，先报 RR。"""
    snap = make_snap(tst=Posture.FAVORED)
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        tradable=False,  # 无结构前高 → RR=1.0<1.5
    )
    assert cand.reason == RejectReason.RR_BELOW_MIN.value


# =========================================================================
# 边界：to_feedback 不回写
# =========================================================================
def test_to_feedback_does_not_mutate_input():
    """to_feedback 仅审计，不改 SignalCandidate / snapshot。"""
    snap = make_snap(tst=Posture.FAVORED)
    cand = judge(
        snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.50,
        structural_target=_STRUCT,
    )
    before = (cand.decision, cand.planned_stop, cand.reward_risk)
    fb = to_feedback(cand)
    after = (cand.decision, cand.planned_stop, cand.reward_risk)
    assert before == after
    assert fb.signal_decision == "accepted"
    assert snap.tst_posture == Posture.FAVORED


def test_to_feedback_rejected():
    snap = make_snap(tst=Posture.BLOCKED)
    cand = judge(snap, family=SetupFamily.TST, expected_entry=10.0, t0_low=9.0)
    fb = to_feedback(cand)
    assert fb.signal_decision == "rejected"
