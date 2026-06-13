"""Signal 裁决引擎（docs/02-module-design/BACKTEST_DESIGN.md §3 §2 规则 3–4）。

读 PAS posture + 质量门 + 风报比规则，独立做 accept/reject，产 SignalCandidate。
纯函数、确定性、无 I/O；reject 结果可转 SignalFeedback 供审计，绝不回写 PAS/MALF。

判定顺序（D4 质量门插在 posture 之后、RR 之前）：
  1. family ∈ accept_families？否 → reject FAMILY_NOT_ACCEPTED
  2. 该 family posture ∈ accepted_postures？否 → reject POSTURE_NOT_ALLOWED
  3. PAS 质量门（许佳冲「2+N」）→ reject READ_STATUS_TOO_WEAK/QUALITY_BELOW_MIN/AMBIGUITY_DOMINATES
  4. 算计划值（stop/1R/结构 T1·T2/可变 RR，调 compute_structural_targets）
  5. reward_risk ≥ min_reward_risk？否 → reject RR_BELOW_MIN
  6. A 股可交易（非停牌/有流动性）？否 → reject NOT_TRADABLE
  7. 否则 accept
"""

from __future__ import annotations

from datetime import date

from asteria.pas.types import DirectionalPremise, PASCoreSnapshot, Posture, ReadStatus, SetupFamily
from asteria.signal.structural import compute_structural_targets
from asteria.signal.types import (
    DEFAULT_CONFIG,
    RejectReason,
    SignalCandidate,
    SignalConfig,
    SignalDecision,
)

# family → PASCoreSnapshot 上的 posture 属性名
_FAMILY_POSTURE_ATTR: dict[SetupFamily, str] = {
    SetupFamily.TST: "tst_posture",
    SetupFamily.BOF: "bof_posture",
    SetupFamily.BPB: "bpb_posture",
    SetupFamily.PB: "pb_posture",
    SetupFamily.CPB: "cpb_posture",
}


def _posture_for_family(snap: PASCoreSnapshot, family: SetupFamily) -> Posture | None:
    """取该 family 在快照里的 posture。"""
    return getattr(snap, _FAMILY_POSTURE_ATTR[family])


def _quality_gate(
    snap: PASCoreSnapshot,
    posture: Posture,
    cfg: SignalConfig,
    life_state: str | None = None,
) -> RejectReason | None:
    """PAS 质量门（D4「2+N 评级」）。返回拒绝原因，或 None 表示通过。

    基本条件：read_status ∈ accepted_read_status（信号强度维度）。
    life_state 上限：排除衰竭/末端（terminal/stagnant），挡掉顶部 climax；
                     life_state=None（transition/样本不足）不卡，留给 RR/posture 决定。
    评分（满分 5）：posture=favored / read=strong / premise 可操作 /
                    证据偏强(strength>weakness) / 无 C6 旗标。
    score < min_quality_score → QUALITY_BELOW_MIN。
    """
    # 基本条件：信号强度可接受
    if snap.read_status not in cfg.accepted_read_status:
        return RejectReason.READ_STATUS_TOO_WEAK
    # life_state 上限：仅当配置了白名单且本 bar 有 life_state 时卡（None 放行）
    if (
        cfg.accepted_life_states is not None
        and life_state is not None
        and life_state not in cfg.accepted_life_states
    ):
        return RejectReason.LIFE_STATE_EXHAUSTED
    # 可选硬否决：歧义主导
    if cfg.veto_ambiguity_dominates and snap.ambiguity_dominates:
        return RejectReason.AMBIGUITY_DOMINATES
    score = 0
    if posture == Posture.FAVORED:
        score += 1
    if snap.read_status == ReadStatus.STRONG:
        score += 1
    if snap.directional_premise in cfg.actionable_premises:
        score += 1
    if snap.strength_evidence_count > snap.weakness_evidence_count:
        score += 1
    if not (snap.transition_bound or snap.lineage_gap or snap.ambiguity_dominates):
        score += 1
    if score < cfg.min_quality_score:
        return RejectReason.QUALITY_BELOW_MIN
    return None


def _round2(x: float) -> float:
    return round(x, 2)


def judge(
    snap: PASCoreSnapshot,
    *,
    family: SetupFamily,
    expected_entry: float,
    t0_low: float,
    structural_target: float | None = None,
    structural_guard: float | None = None,
    life_state: str | None = None,
    cfg: SignalConfig = DEFAULT_CONFIG,
    tradable: bool = True,
    discover_dt: date | None = None,
    lifespan_id: str | None = None,
    source_run_id: str | None = None,
) -> SignalCandidate:
    """对单 (snapshot, family) 裁决，返回 SignalCandidate（含计划值与 decision）。

    判定顺序（D4 质量门 + D3 可变 RR）：
      1 family ∈ accept_families        else FAMILY_NOT_ACCEPTED
      2 posture ∈ accepted_postures     else POSTURE_NOT_ALLOWED
      3 PAS 质量门                       else READ_STATUS_TOO_WEAK / QUALITY_BELOW_MIN / AMBIGUITY_DOMINATES
      4 算 stop/结构T1T2/可变RR
      5 reward_risk ≥ min_reward_risk   else RR_BELOW_MIN
      6 tradable                        else NOT_TRADABLE
      7 accept

    expected_entry：T1 预期开盘价（计划值，引擎进场后按实际 fill 重算权威值）。
    t0_low：T0（发现日）最低价，初始止损 = t0_low - stop_offset。
    structural_target：MALF progress_extreme_price（前高/最近阻力），RR 与 T1 的结构锚。
    structural_guard：MALF guard_price（最近确认 HL），T2 量度移动的下锚。
    """
    discover_dt = discover_dt or snap.bar_dt

    def _make(
        decision: SignalDecision,
        reason: str,
        *,
        planned_entry: float | None = None,
        planned_stop: float | None = None,
        planned_target1: float | None = None,
        planned_target2: float | None = None,
        reward_risk: float | None = None,
    ) -> SignalCandidate:
        return SignalCandidate(
            symbol=snap.symbol,
            timeframe=snap.timeframe,
            discover_dt=discover_dt,
            setup_family=family,
            directional_premise=snap.directional_premise,
            read_status=snap.read_status,
            planned_entry=planned_entry,
            planned_stop=planned_stop,
            planned_target1=planned_target1,
            planned_target2=planned_target2,
            reward_risk=reward_risk,
            decision=decision,
            reason=reason,
            lifespan_id=lifespan_id,
            signal_rule_version=cfg.signal_rule_version,
            source_run_id=source_run_id,
        )

    # 1. family 过滤
    if family not in cfg.accept_families:
        return _make(SignalDecision.REJECT, RejectReason.FAMILY_NOT_ACCEPTED.value)

    # 2. posture 过滤
    posture = _posture_for_family(snap, family)
    if posture is None or posture not in cfg.accepted_postures:
        return _make(SignalDecision.REJECT, RejectReason.POSTURE_NOT_ALLOWED.value)

    # 3. PAS 质量门（D4「2+N」+ life_state 上限）
    quality_reject = _quality_gate(snap, posture, cfg, life_state)
    if quality_reject is not None:
        return _make(SignalDecision.REJECT, quality_reject.value)

    # 4. 计划值 + 可变 RR（D3：RR 对结构前高算）
    raw_stop = _round2(t0_low - cfg.stop_offset)
    if expected_entry - raw_stop <= 0:
        # 风险单位非正（entry 已在 stop 之下）→ 不可交易语义，记为 RR 不达标
        return _make(
            SignalDecision.REJECT,
            RejectReason.RR_BELOW_MIN.value,
            planned_entry=expected_entry,
            planned_stop=raw_stop,
        )
    targets = compute_structural_targets(
        entry=expected_entry,
        stop=raw_stop,
        progress_extreme=structural_target,
        guard=structural_guard,
        target_r=cfg.target_r,
        min_risk_pct=cfg.min_risk_pct,
    )
    # 用 effective 值（min_risk 地板后）：stop/1R/RR 一致，修复极小分母 RR 虚高
    stop = targets.effective_stop
    reward_risk = (targets.rr_target - expected_entry) / targets.risk_unit

    # 5. 风报比门槛（D3：无结构前高 → RR 退回 1.0 < 1.5 → 拒绝）
    if reward_risk < cfg.min_reward_risk:
        return _make(
            SignalDecision.REJECT,
            RejectReason.RR_BELOW_MIN.value,
            planned_entry=expected_entry,
            planned_stop=stop,
            planned_target1=targets.target1,
            planned_target2=targets.target2,
            reward_risk=reward_risk,
        )

    # 6. A 股可交易
    if not tradable:
        return _make(
            SignalDecision.REJECT,
            RejectReason.NOT_TRADABLE.value,
            planned_entry=expected_entry,
            planned_stop=stop,
            planned_target1=targets.target1,
            planned_target2=targets.target2,
            reward_risk=reward_risk,
        )

    # 7. accept
    return _make(
        SignalDecision.ACCEPT,
        "accepted",
        planned_entry=expected_entry,
        planned_stop=stop,
        planned_target1=targets.target1,
        planned_target2=targets.target2,
        reward_risk=reward_risk,
    )


def to_feedback(cand: SignalCandidate):
    """把裁决结果转成 PAS 审计反馈（SignalFeedback）。仅审计/统计/replay，绝不回写。"""
    from asteria.pas.types import SignalFeedback

    return SignalFeedback(
        symbol=cand.symbol,
        timeframe=cand.timeframe,
        bar_dt=cand.discover_dt,
        lifespan_id=cand.lifespan_id or "",
        signal_decision="accepted" if cand.decision == SignalDecision.ACCEPT else "rejected",
        decision_reason=cand.reason,
        signal_rule_version=cand.signal_rule_version,
        source_run_id=cand.source_run_id,
    )
