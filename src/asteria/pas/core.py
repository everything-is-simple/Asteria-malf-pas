"""PAS v1.5 Core 确定性流水线（PAS_01B C2）。

输入 = WavePosition + WaveBehaviorSnapshot（逐 bar 一一对应）。
输出 = PASCoreSnapshot（premise + read + 5 族 posture）。

🔒 边界铁律：
- 禁读 PriceBar、禁重算 MALF。
- 内部三态树标签不发布。
- 输出不含 accept/reject/order/position。

流水线顺序（C2，不可跳步）：
  1. 校验 MALF input contract
  2. 内部三态树（IA-1~4）
  3. 映射 DirectionalPremise（IA-5）
  4. 收集 EvidenceTriplet
  5. 确定 ReadStatus
  6. Posture Matrix（PM-T1~6）
  7. C6 上限约束
  8. 发布 PASCoreSnapshot
"""

from __future__ import annotations

from dataclasses import dataclass

from asteria.malf.types import (
    BoundaryPressureRegime,
    ContinuationRegime,
    LifeState,
    StagnationRegime,
    SystemState,
    WaveBehaviorSnapshot,
    WavePosition,
)
from asteria.pas.types import (
    DirectionalPremise,
    PASCoreSnapshot,
    Posture,
    ReadStatus,
    _InternalTriState,
    _NeutralSubtype,
)

PAS_CORE_RULE_VERSION = "pas-core-v1.5-mvp"


@dataclass(frozen=True)
class PASConfig:
    """PAS Core 硬阈值（IA-3/IA-4 写死值）。"""

    # IA-3 Proving 条件
    proving_max_no_new_span: int = 5  # no_new_span < 5
    # IA-4 Neutral subtype
    terminal_observation_min_no_new_span: int = 20
    stagnant_min_no_new_span: int = 10
    pas_core_rule_version: str = PAS_CORE_RULE_VERSION


DEFAULT_CONFIG = PASConfig()


# =========================================================================
# 主入口
# =========================================================================
def derive_pas_snapshots(
    positions: list[WavePosition],
    behaviors: list[WaveBehaviorSnapshot],
    *,
    cfg: PASConfig = DEFAULT_CONFIG,
    source_run_id: str = "adhoc",
) -> list[PASCoreSnapshot]:
    """逐 bar 派生 PASCoreSnapshot。

    positions 与 behaviors 必须逐 bar 一一对应（同 symbol/timeframe/bar_dt，同序）。
    """
    if len(positions) != len(behaviors):
        raise ValueError(
            f"positions({len(positions)}) 与 behaviors({len(behaviors)}) 长度不一致"
        )

    out: list[PASCoreSnapshot] = []
    for pos, beh in zip(positions, behaviors):
        out.append(_derive_one(pos, beh, cfg, source_run_id))
    return out


def _derive_one(
    pos: WavePosition,
    beh: WaveBehaviorSnapshot,
    cfg: PASConfig,
    source_run_id: str,
) -> PASCoreSnapshot:
    """单 bar 派生（C2 流水线）。"""
    reasons: list[str] = []

    # 1. 校验 input contract
    if pos.symbol != beh.symbol or pos.bar_dt != beh.bar_dt:
        raise ValueError(f"pos 与 beh 不对齐：{pos.symbol}/{pos.bar_dt} vs {beh.symbol}/{beh.bar_dt}")

    # 2. 内部三态树（IA-1~4）
    tri_state, neutral_sub = _internal_tri_state(pos, beh, cfg, reasons)

    # 3. 映射 DirectionalPremise（IA-5）
    premise = _map_premise(tri_state, neutral_sub, reasons)

    # 4. 收集 EvidenceTriplet
    strength_count, weakness_count, ambiguity_count = _collect_evidence(beh, reasons)

    # 5. 确定 ReadStatus
    read_status = _determine_read_status(
        strength_count, weakness_count, ambiguity_count, reasons
    )

    # 6. Posture Matrix（PM-T1~6 基础查表）
    tst, bof, bpb, pb, cpb = _posture_matrix(premise, read_status, reasons)

    # 7. C6 上限约束
    transition_bound = pos.system_state == SystemState.TRANSITION
    lineage_gap = pos.wave_id is None
    ambiguity_dominates = ambiguity_count > (strength_count + weakness_count)
    tst, bof, bpb, pb, cpb = _apply_c6_ceiling(
        tst, bof, bpb, pb, cpb,
        transition_bound, lineage_gap, ambiguity_dominates, premise, reasons
    )

    lineage = f"{source_run_id}|{cfg.pas_core_rule_version}"

    return PASCoreSnapshot(
        symbol=pos.symbol,
        timeframe=pos.timeframe,
        bar_dt=pos.bar_dt,
        wave_id=pos.wave_id,
        direction=pos.direction,
        system_state=pos.system_state.value if pos.system_state else None,
        directional_premise=premise,
        read_status=read_status,
        tst_posture=tst,
        bof_posture=bof,
        bpb_posture=bpb,
        pb_posture=pb,
        cpb_posture=cpb,
        transition_bound=transition_bound,
        lineage_gap=lineage_gap,
        ambiguity_dominates=ambiguity_dominates,
        strength_evidence_count=strength_count,
        weakness_evidence_count=weakness_count,
        ambiguity_evidence_count=ambiguity_count,
        reason_codes=reasons,
        lineage_hash=lineage,
        pas_core_rule_version=cfg.pas_core_rule_version,
        source_run_id=source_run_id,
    )


# =========================================================================
# 内部三态树（IA-1~4）
# =========================================================================
def _internal_tri_state(
    pos: WavePosition,
    beh: WaveBehaviorSnapshot,
    cfg: PASConfig,
    reasons: list[str],
) -> tuple[_InternalTriState, _NeutralSubtype | None]:
    """IA-1~4：Denying / Proving / Neutral（取编号最小）。

    返回 (tri_state, neutral_subtype)，neutral_subtype 仅 NEUTRAL 时非 None。
    """
    # IA-1 Denying（任一成立）
    if _is_denying(pos, beh, reasons):
        return (_InternalTriState.DENYING, None)

    # IA-2 Proving（8 条全成立）
    if _is_proving(pos, beh, cfg, reasons):
        return (_InternalTriState.PROVING, None)

    # IA-4 Neutral subtype（按编号取最小）
    neutral_sub = _neutral_subtype(pos, beh, cfg, reasons)
    return (_InternalTriState.NEUTRAL, neutral_sub)


def _is_denying(
    pos: WavePosition, beh: WaveBehaviorSnapshot, reasons: list[str]
) -> bool:
    """IA-1 Denying 条件（任一成立）。"""
    # system_state = transition
    if pos.system_state == SystemState.TRANSITION:
        reasons.append("tri:denying(system_state=transition)")
        return True
    # guard broken / boundary=guard_pressure（规范 IA-2 的两个触发器在 PAS 输入契约下同源：
    # PAS 只读 WaveBehaviorSnapshot，guard broken 的唯一信号就是 boundary_pressure_regime=guard_pressure）
    if beh.boundary_pressure_regime == BoundaryPressureRegime.GUARD_PRESSURE:
        reasons.append("tri:denying(guard_broken/boundary=guard_pressure)")
        return True
    # life_state = terminal
    if pos.life_state == LifeState.TERMINAL:
        reasons.append("tri:denying(life_state=terminal)")
        return True
    # continuation = transitioning
    if beh.continuation_regime == ContinuationRegime.TRANSITIONING:
        reasons.append("tri:denying(continuation=transitioning)")
        return True
    # stagnation = terminal_pressure
    if beh.stagnation_regime == StagnationRegime.TERMINAL_PRESSURE:
        reasons.append("tri:denying(stagnation=terminal_pressure)")
        return True
    return False


def _is_proving(
    pos: WavePosition, beh: WaveBehaviorSnapshot, cfg: PASConfig, reasons: list[str]
) -> bool:
    """IA-2 Proving 条件（8 条全成立）。"""
    checks = []
    # 1. system_state ∈ {up_alive, down_alive}
    if pos.system_state not in (SystemState.UP_ALIVE, SystemState.DOWN_ALIVE):
        return False
    checks.append("system_state=alive")
    # 2. guard intact
    if beh.boundary_pressure_regime != BoundaryPressureRegime.CONTINUATION_SIDE:
        return False
    checks.append("guard_intact")
    # 3. new_count >= 1
    if pos.new_count < 1:
        return False
    checks.append("new_count>=1")
    # 4. life_state ∈ {early, developing}
    if pos.life_state not in (LifeState.EARLY, LifeState.DEVELOPING):
        return False
    checks.append("life_state∈{early,developing}")
    # 5. no_new_span < 5（IA-3 硬阈值）
    if pos.no_new_span >= cfg.proving_max_no_new_span:
        return False
    checks.append(f"no_new_span<{cfg.proving_max_no_new_span}")
    # 6. continuation = advancing
    if beh.continuation_regime != ContinuationRegime.ADVANCING:
        return False
    checks.append("continuation=advancing")
    # 7. stagnation ∈ {fresh, watchful}
    if beh.stagnation_regime not in (StagnationRegime.FRESH, StagnationRegime.WATCHFUL):
        return False
    checks.append("stagnation∈{fresh,watchful}")
    # 8. boundary = continuation_side
    if beh.boundary_pressure_regime != BoundaryPressureRegime.CONTINUATION_SIDE:
        return False
    checks.append("boundary=continuation_side")

    reasons.append(f"tri:proving({','.join(checks)})")
    return True


def _neutral_subtype(
    pos: WavePosition, beh: WaveBehaviorSnapshot, cfg: PASConfig, reasons: list[str]
) -> _NeutralSubtype:
    """IA-4 Neutral 五子类（按编号取最小）。"""
    # 1. terminal_observation: no_new_span >= 20
    if pos.no_new_span >= cfg.terminal_observation_min_no_new_span:
        reasons.append(f"tri:neutral:terminal_observation(no_new_span={pos.no_new_span}>=20)")
        return _NeutralSubtype.TERMINAL_OBSERVATION
    # 2. stagnant: no_new_span >= 10 或 stalled
    if (
        pos.no_new_span >= cfg.stagnant_min_no_new_span
        or beh.stagnation_regime == StagnationRegime.STALLED
    ):
        reasons.append(f"tri:neutral:stagnant(no_new_span={pos.no_new_span}>=10 or stalled)")
        return _NeutralSubtype.STAGNANT
    # 3. slowing: continuation = slowing
    if beh.continuation_regime == ContinuationRegime.SLOWING:
        reasons.append("tri:neutral:slowing(continuation=slowing)")
        return _NeutralSubtype.SLOWING
    # 4. newborn: new_count = 0
    if pos.new_count == 0:
        reasons.append("tri:neutral:newborn(new_count=0)")
        return _NeutralSubtype.NEWBORN
    # 5. watchful（其余）
    reasons.append("tri:neutral:watchful(fallback)")
    return _NeutralSubtype.WATCHFUL


# =========================================================================
# DirectionalPremise 映射（IA-5）
# =========================================================================
def _map_premise(
    tri_state: _InternalTriState,
    neutral_sub: _NeutralSubtype | None,
    reasons: list[str],
) -> DirectionalPremise:
    """IA-5：三态树 → DirectionalPremise。"""
    if tri_state == _InternalTriState.DENYING:
        reasons.append("premise:expect_weakness_rejection(denying)")
        return DirectionalPremise.EXPECT_WEAKNESS_REJECTION
    if tri_state == _InternalTriState.PROVING:
        reasons.append("premise:expect_strength_continuation(proving)")
        return DirectionalPremise.EXPECT_STRENGTH_CONTINUATION
    # NEUTRAL：按 subtype 映射
    if neutral_sub == _NeutralSubtype.TERMINAL_OBSERVATION:
        reasons.append("premise:no_actionable_premise(terminal_observation)")
        return DirectionalPremise.NO_ACTIONABLE_PREMISE
    if neutral_sub == _NeutralSubtype.STAGNANT:
        reasons.append("premise:expect_boundary_test(stagnant)")
        return DirectionalPremise.EXPECT_BOUNDARY_TEST
    if neutral_sub == _NeutralSubtype.SLOWING:
        reasons.append("premise:expect_boundary_test(slowing)")
        return DirectionalPremise.EXPECT_BOUNDARY_TEST
    if neutral_sub == _NeutralSubtype.NEWBORN:
        reasons.append("premise:no_actionable_premise(newborn)")
        return DirectionalPremise.NO_ACTIONABLE_PREMISE
    # watchful
    reasons.append("premise:expect_transition_resolution(watchful)")
    return DirectionalPremise.EXPECT_TRANSITION_RESOLUTION


# =========================================================================
# EvidenceTriplet 收集
# =========================================================================
def _collect_evidence(
    beh: WaveBehaviorSnapshot, reasons: list[str]
) -> tuple[int, int, int]:
    """收集 strength/weakness/ambiguity 证据计数（从 6 regime 读取）。

    简化实现：命中 regime 名列表。
    """
    strength = 0
    weakness = 0
    ambiguity = 0

    # continuation
    if beh.continuation_regime == ContinuationRegime.ADVANCING:
        strength += 1
    elif beh.continuation_regime == ContinuationRegime.STALLED:
        weakness += 1
    elif beh.continuation_regime in (ContinuationRegime.SLOWING, ContinuationRegime.TRANSITIONING):
        ambiguity += 1

    # stagnation
    if beh.stagnation_regime == StagnationRegime.FRESH:
        strength += 1
    elif beh.stagnation_regime in (StagnationRegime.STALLED, StagnationRegime.TERMINAL_PRESSURE):
        weakness += 1
    elif beh.stagnation_regime == StagnationRegime.WATCHFUL:
        ambiguity += 1

    # boundary
    if beh.boundary_pressure_regime == BoundaryPressureRegime.CONTINUATION_SIDE:
        strength += 1
    elif beh.boundary_pressure_regime == BoundaryPressureRegime.GUARD_PRESSURE:
        weakness += 1
    elif beh.boundary_pressure_regime in (BoundaryPressureRegime.TRANSITION_PRESSURE, BoundaryPressureRegime.NEUTRAL):
        ambiguity += 1

    reasons.append(f"evidence:strength={strength},weakness={weakness},ambiguity={ambiguity}")
    return strength, weakness, ambiguity


# =========================================================================
# ReadStatus 确定
# =========================================================================
def _determine_read_status(
    strength: int, weakness: int, ambiguity: int, reasons: list[str]
) -> ReadStatus:
    """从 EvidenceTriplet 派生 ReadStatus。"""
    total = strength + weakness + ambiguity
    if total == 0:
        reasons.append("read:not_applicable(no_evidence)")
        return ReadStatus.NOT_APPLICABLE
    # ambiguity 主导
    if ambiguity > (strength + weakness):
        reasons.append("read:ambiguous(ambiguity_dominates)")
        return ReadStatus.AMBIGUOUS
    # strength vs weakness
    if strength > weakness:
        reasons.append(f"read:strong(strength={strength}>weakness={weakness})")
        return ReadStatus.STRONG
    if weakness > strength:
        reasons.append(f"read:weak(weakness={weakness}>strength={strength})")
        return ReadStatus.WEAK
    # 平局
    reasons.append(f"read:mixed(strength={strength}==weakness={weakness})")
    return ReadStatus.MIXED


# =========================================================================
# Posture Matrix（PM-T1~6）
# =========================================================================
def _posture_matrix(
    premise: DirectionalPremise,
    read_status: ReadStatus,
    reasons: list[str],
) -> tuple[Posture, Posture, Posture, Posture, Posture]:
    """PM-T1~6 基础查表（不含 C6 上限约束）。

    返回 (TST, BOF, BPB, PB, CPB)。
    """
    # PM-T1: strength_continuation + strong
    if premise == DirectionalPremise.EXPECT_STRENGTH_CONTINUATION and read_status == ReadStatus.STRONG:
        reasons.append("pm:T1(strength_continuation+strong)")
        return (Posture.ALLOWED, Posture.BLOCKED, Posture.FAVORED, Posture.FAVORED, Posture.DEFERRED)

    # PM-T2: weakness_rejection + weak
    if premise == DirectionalPremise.EXPECT_WEAKNESS_REJECTION and read_status == ReadStatus.WEAK:
        reasons.append("pm:T2(weakness_rejection+weak)")
        return (Posture.ALLOWED, Posture.FAVORED, Posture.BLOCKED, Posture.BLOCKED, Posture.DEFERRED)

    # PM-T3: boundary_test + mixed
    if premise == DirectionalPremise.EXPECT_BOUNDARY_TEST and read_status == ReadStatus.MIXED:
        reasons.append("pm:T3(boundary_test+mixed)")
        return (Posture.FAVORED, Posture.ALLOWED, Posture.DEFERRED, Posture.DEFERRED, Posture.DEFERRED)

    # PM-T4: transition_resolution + ambiguous
    if premise == DirectionalPremise.EXPECT_TRANSITION_RESOLUTION and read_status == ReadStatus.AMBIGUOUS:
        reasons.append("pm:T4(transition_resolution+ambiguous)")
        return (Posture.DEFERRED, Posture.DEFERRED, Posture.BLOCKED, Posture.BLOCKED, Posture.BLOCKED)

    # PM-T5: no_actionable_premise / not_applicable（规范：premise=no_actionable 或 read_status=not_applicable）
    if (
        premise in (DirectionalPremise.NO_ACTIONABLE_PREMISE, DirectionalPremise.NOT_APPLICABLE)
        or read_status == ReadStatus.NOT_APPLICABLE
    ):
        reasons.append("pm:T5(no_actionable_premise/not_applicable)")
        return (Posture.BLOCKED, Posture.BLOCKED, Posture.BLOCKED, Posture.BLOCKED, Posture.BLOCKED)

    # PM-T6: ReadStatus 与 Premise 不匹配 → 全体降一档
    reasons.append(f"pm:T6(mismatch:{premise.value}+{read_status.value})")
    base = _downgrade_all(_base_posture_for_premise(premise))
    return base


def _base_posture_for_premise(premise: DirectionalPremise) -> tuple[Posture, Posture, Posture, Posture, Posture]:
    """根据 Premise 给出基础 posture（用于 PM-T6 降档前）。"""
    if premise == DirectionalPremise.EXPECT_STRENGTH_CONTINUATION:
        return (Posture.ALLOWED, Posture.BLOCKED, Posture.FAVORED, Posture.FAVORED, Posture.DEFERRED)
    if premise == DirectionalPremise.EXPECT_WEAKNESS_REJECTION:
        return (Posture.ALLOWED, Posture.FAVORED, Posture.BLOCKED, Posture.BLOCKED, Posture.DEFERRED)
    if premise == DirectionalPremise.EXPECT_BOUNDARY_TEST:
        return (Posture.FAVORED, Posture.ALLOWED, Posture.DEFERRED, Posture.DEFERRED, Posture.DEFERRED)
    if premise == DirectionalPremise.EXPECT_TRANSITION_RESOLUTION:
        return (Posture.DEFERRED, Posture.DEFERRED, Posture.BLOCKED, Posture.BLOCKED, Posture.BLOCKED)
    return (Posture.BLOCKED, Posture.BLOCKED, Posture.BLOCKED, Posture.BLOCKED, Posture.BLOCKED)


def _downgrade_all(base: tuple[Posture, Posture, Posture, Posture, Posture]) -> tuple[Posture, Posture, Posture, Posture, Posture]:
    """PM-T6：全体降一档（favored→allowed→deferred→blocked，只降一次）。"""
    return tuple(_downgrade_one(p) for p in base)


def _downgrade_one(p: Posture) -> Posture:
    """单档降级。"""
    if p == Posture.FAVORED:
        return Posture.ALLOWED
    if p == Posture.ALLOWED:
        return Posture.DEFERRED
    if p == Posture.DEFERRED:
        return Posture.BLOCKED
    return Posture.BLOCKED


# =========================================================================
# C6 上限约束
# =========================================================================
def _apply_c6_ceiling(
    tst: Posture, bof: Posture, bpb: Posture, pb: Posture, cpb: Posture,
    transition_bound: bool,
    lineage_gap: bool,
    ambiguity_dominates: bool,
    premise: DirectionalPremise,
    reasons: list[str],
) -> tuple[Posture, Posture, Posture, Posture, Posture]:
    """C6 上限约束。

    - transition_bound / lineage_gap / ambiguity 主导 → 上限 deferred
    - 无 lineage / premise=no_actionable → 全 blocked
    """
    # 无 lineage / premise=no_actionable → 全 blocked
    if lineage_gap or premise == DirectionalPremise.NO_ACTIONABLE_PREMISE:
        reasons.append("c6:all_blocked(lineage_gap or no_actionable)")
        return (Posture.BLOCKED, Posture.BLOCKED, Posture.BLOCKED, Posture.BLOCKED, Posture.BLOCKED)

    # transition_bound / ambiguity 主导 → 上限 deferred
    if transition_bound or ambiguity_dominates:
        reasons.append("c6:cap_deferred(transition_bound or ambiguity_dominates)")
        return (
            _cap_deferred(tst),
            _cap_deferred(bof),
            _cap_deferred(bpb),
            _cap_deferred(pb),
            _cap_deferred(cpb),
        )

    return (tst, bof, bpb, pb, cpb)


def _cap_deferred(p: Posture) -> Posture:
    """上限 deferred：favored/allowed → deferred；deferred/blocked 不变。"""
    if p in (Posture.FAVORED, Posture.ALLOWED):
        return Posture.DEFERRED
    return p
