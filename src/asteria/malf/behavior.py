"""MALF v1.5 行为层：六个 regime 纯派生（MALF_03 / 01B）。

输入 = WavePosition[] + CoreStateSnapshot[]（逐 bar 一一对应，同序）。
输出 = WaveBehaviorSnapshot[]，只读·非决策。

🔒 派生铁律（v1.5 01B §2）：
- 只能从 v1.4 已确认结构事实派生，不得引入强弱评分。
- transition 优先于一切延续 bucket。
- 只有 system_state != transition 才给 active-wave continuation bucket。
- 无 current_effective_guard → 不输出 guard_pressure。
- 无 confirmation_distance_* → 不输出 birth_quality（落 unknown_birth）。

🔒 Service 铁律（MALF_03 §4）：本层永不输出 strength_score / setup_family /
accept-reject / order / position / fill / profit。

派生顺序（v1.5 01B §1，不允许跳步）：
  读 core state → 读 wave_position → 读 transition/birth lineage
  → 派生 6 bucket → 附 reason_codes + rule version → 发布只读快照。
"""

from __future__ import annotations

from dataclasses import dataclass

from asteria.malf.core import CORE_RULE_VERSION
from asteria.malf.types import (
    BirthQualityRegime,
    BirthType,
    BoundaryPressureRegime,
    ContinuationRegime,
    CoreStateSnapshot,
    DirectionalContinuityRegime,
    LifeState,
    StagnationRegime,
    SystemState,
    TransitionRegime,
    WaveBehaviorSnapshot,
    WaveCoreState,
    WavePosition,
)

BEHAVIOR_RULE_VERSION = "malf-behavior-v1.5-mvp"


@dataclass(frozen=True)
class BehaviorConfig:
    """行为 regime 分界阈值（默认值同 config/params_default.toml 的 [malf.behavior]）。

    用 dataclass 承载便于测试注入；不读 toml，避免引入配置加载依赖。
    """

    # continuation：no_new_span 阈值
    advancing_max_no_new_span: int = 2   # <= 此值且 new_count>=1 → advancing
    slowing_max_no_new_span: int = 5     # (advancing, slowing] → slowing
    stalled_min_no_new_span: int = 10    # >= 此值 → stalled
    # stagnation：no_new_span 阈值
    fresh_max_no_new_span: int = 2       # <= 此值 → fresh
    watchful_max_no_new_span: int = 5    # (fresh, watchful] → watchful
    # transition：candidate 替换次数判 replacement_heavy
    replacement_heavy_min: int = 2       # candidate_replacement_count >= 此值 → replacement_heavy
    prolonged_transition_min_span: int = 5  # transition_span >= 此值 → prolonged_unresolved
    # birth_quality：替换次数判 negotiated/costly
    negotiated_birth_min_replacement: int = 1  # >=1 次替换 → negotiated
    costly_birth_min_replacement: int = 2      # >=2 次替换 → costly
    behavior_rule_version: str = BEHAVIOR_RULE_VERSION


DEFAULT_CONFIG = BehaviorConfig()


# =========================================================================
# 主入口
# =========================================================================
def derive_behavior_snapshots(
    positions: list[WavePosition],
    core_snaps: list[CoreStateSnapshot],
    *,
    cfg: BehaviorConfig = DEFAULT_CONFIG,
    source_run_id: str = "adhoc",
) -> list[WaveBehaviorSnapshot]:
    """逐 bar 派生 6 regime → WaveBehaviorSnapshot。

    positions 与 core_snaps 必须逐 bar 一一对应（同 symbol/timeframe，同序）。
    """
    if len(positions) != len(core_snaps):
        raise ValueError(
            f"positions({len(positions)}) 与 core_snaps({len(core_snaps)}) 长度不一致，无法逐 bar 对齐"
        )

    out: list[WaveBehaviorSnapshot] = []
    for pos, snap in zip(positions, core_snaps):
        out.append(_derive_one(pos, snap, cfg, source_run_id))
    return out


def _derive_one(
    pos: WavePosition,
    snap: CoreStateSnapshot,
    cfg: BehaviorConfig,
    source_run_id: str,
) -> WaveBehaviorSnapshot:
    reasons: list[str] = []

    is_transition = snap.system_state == SystemState.TRANSITION
    is_uninit = snap.system_state == SystemState.UNINITIALIZED

    continuation = _continuation_regime(pos, snap, cfg, is_transition, is_uninit, reasons)
    stagnation = _stagnation_regime(pos, cfg, is_transition, is_uninit, reasons)
    directional = _directional_continuity_regime(pos, snap, is_transition, is_uninit, reasons)
    boundary = _boundary_pressure_regime(pos, snap, is_transition, is_uninit, reasons)
    transition = _transition_regime(pos, snap, cfg, is_transition, reasons)
    birth = _birth_quality_regime(pos, cfg, reasons)

    lineage = f"{source_run_id}|{CORE_RULE_VERSION}|{cfg.behavior_rule_version}"

    return WaveBehaviorSnapshot(
        symbol=pos.symbol,
        timeframe=pos.timeframe,
        bar_dt=pos.bar_dt,
        wave_id=pos.wave_id,
        direction=pos.direction,
        old_wave_id=pos.old_wave_id,
        open_transition_id=snap.open_transition_id,
        continuation_regime=continuation,
        directional_continuity_regime=directional,
        stagnation_regime=stagnation,
        boundary_pressure_regime=boundary,
        transition_regime=transition,
        birth_quality_regime=birth,
        reason_codes=reasons,
        lineage_hash=lineage,
        malf_v1_4_rule_version=CORE_RULE_VERSION,
        malf_v1_5_rule_version=cfg.behavior_rule_version,
        source_run_id=source_run_id,
    )


# =========================================================================
# 六个 regime 派生（逐一对照设计 §4.2 来源字段）
# =========================================================================
def _continuation_regime(
    pos: WavePosition,
    snap: CoreStateSnapshot,
    cfg: BehaviorConfig,
    is_transition: bool,
    is_uninit: bool,
    reasons: list[str],
) -> ContinuationRegime | None:
    """C1：system_state, new_count, no_new_span。

    🔒 transition 优先：transition 期 → transitioning，不给 active-wave bucket。
    """
    if is_transition:
        reasons.append("cont:transitioning(transition_priority)")
        return ContinuationRegime.TRANSITIONING
    if is_uninit:
        return None  # 无活波，不输出延续 bucket
    nns = pos.no_new_span
    if nns >= cfg.stalled_min_no_new_span:
        reasons.append(f"cont:stalled(no_new_span={nns}>={cfg.stalled_min_no_new_span})")
        return ContinuationRegime.STALLED
    if nns <= cfg.advancing_max_no_new_span and pos.new_count >= 1:
        reasons.append(f"cont:advancing(no_new_span={nns}<={cfg.advancing_max_no_new_span})")
        return ContinuationRegime.ADVANCING
    if nns <= cfg.slowing_max_no_new_span:
        reasons.append(f"cont:slowing(no_new_span={nns}<={cfg.slowing_max_no_new_span})")
        return ContinuationRegime.SLOWING
    # 落在 slowing 上界与 stalled 下界之间：仍判 slowing（连续推进减速但未停滞）
    reasons.append(f"cont:slowing(no_new_span={nns}, mid-band)")
    return ContinuationRegime.SLOWING


def _stagnation_regime(
    pos: WavePosition,
    cfg: BehaviorConfig,
    is_transition: bool,
    is_uninit: bool,
    reasons: list[str],
) -> StagnationRegime | None:
    """L1(v1.5)：no_new_span, stagnation_rank, life_state。"""
    if is_uninit:
        return None
    # terminated / terminal life_state → terminal_pressure
    if pos.wave_core_state == WaveCoreState.TERMINATED or pos.life_state == LifeState.TERMINAL:
        reasons.append("stag:terminal_pressure(terminated/terminal)")
        return StagnationRegime.TERMINAL_PRESSURE
    if is_transition:
        # transition 期仍按 no_new_span 冻结值判停滞档（无活波推进）
        reasons.append("stag:watchful(transition)")
        return StagnationRegime.WATCHFUL
    nns = pos.no_new_span
    if nns >= cfg.stalled_min_no_new_span:
        reasons.append(f"stag:stalled(no_new_span={nns}>={cfg.stalled_min_no_new_span})")
        return StagnationRegime.STALLED
    if nns <= cfg.fresh_max_no_new_span:
        reasons.append(f"stag:fresh(no_new_span={nns}<={cfg.fresh_max_no_new_span})")
        return StagnationRegime.FRESH
    if nns <= cfg.watchful_max_no_new_span:
        reasons.append(f"stag:watchful(no_new_span={nns}<={cfg.watchful_max_no_new_span})")
        return StagnationRegime.WATCHFUL
    # mid-band（watchful 上界与 stalled 下界之间）→ watchful
    reasons.append(f"stag:watchful(no_new_span={nns}, mid-band)")
    return StagnationRegime.WATCHFUL


def _directional_continuity_regime(
    pos: WavePosition,
    snap: CoreStateSnapshot,
    is_transition: bool,
    is_uninit: bool,
    reasons: list[str],
) -> DirectionalContinuityRegime | None:
    """C3：direction, old_direction, birth_type, system_state。"""
    if is_uninit:
        return None
    if is_transition:
        reasons.append("dir:transition_unresolved(transition)")
        return DirectionalContinuityRegime.TRANSITION_UNRESOLVED
    # 用 birth_type 判同向/反向重生
    if pos.birth_type == BirthType.OPPOSITE_DIRECTION_AFTER_BREAK:
        reasons.append("dir:opposite_direction_rebirth(birth)")
        return DirectionalContinuityRegime.OPPOSITE_DIRECTION_REBIRTH
    # initial 或 same_direction_after_break 都视为同向延续
    reasons.append("dir:same_direction_continuation(birth)")
    return DirectionalContinuityRegime.SAME_DIRECTION_CONTINUATION


def _boundary_pressure_regime(
    pos: WavePosition,
    snap: CoreStateSnapshot,
    is_transition: bool,
    is_uninit: bool,
    reasons: list[str],
) -> BoundaryPressureRegime | None:
    """C2：与 guard/boundary 的关系。

    🔒 无 current_effective_guard → 不输出 guard_pressure（落 neutral）。
    """
    if is_uninit:
        return None
    if is_transition:
        reasons.append("bound:transition_pressure(transition)")
        return BoundaryPressureRegime.TRANSITION_PRESSURE
    # 活波：有 guard → continuation_side；无 guard → neutral（不可输出 guard_pressure）
    if snap.current_effective_guard_price is None:
        reasons.append("bound:neutral(no_guard)")
        return BoundaryPressureRegime.NEUTRAL
    reasons.append("bound:continuation_side(guard_intact)")
    return BoundaryPressureRegime.CONTINUATION_SIDE


def _transition_regime(
    pos: WavePosition,
    snap: CoreStateSnapshot,
    cfg: BehaviorConfig,
    is_transition: bool,
    reasons: list[str],
) -> TransitionRegime:
    """L2(v1.5)：transition_span, candidate_replacement_count, open_transition_id。

    判定基于「产生当前波的 transition 历史」(birth descriptors) 或当前 open transition。
    无任何 transition 关联 → not_applicable。
    """
    # 当前正处 transition：用 transition_span + 当前候选替换次数（暂用 pos.transition_span）
    if is_transition:
        if pos.transition_span >= cfg.prolonged_transition_min_span:
            reasons.append(
                f"trans:prolonged_unresolved(span={pos.transition_span}>={cfg.prolonged_transition_min_span})"
            )
            return TransitionRegime.PROLONGED_UNRESOLVED
        reasons.append(f"trans:clean_handoff(open,span={pos.transition_span})")
        return TransitionRegime.CLEAN_HANDOFF

    # 活波：看其出生 transition 的替换次数（birth descriptor）
    rep = pos.candidate_replacement_count
    if rep is None:
        # initial wave 或无 transition 出生 → not_applicable
        reasons.append("trans:not_applicable(no_birth_transition)")
        return TransitionRegime.NOT_APPLICABLE
    if rep >= cfg.replacement_heavy_min:
        reasons.append(f"trans:replacement_heavy(rep={rep}>={cfg.replacement_heavy_min})")
        return TransitionRegime.REPLACEMENT_HEAVY
    reasons.append(f"trans:clean_handoff(rep={rep})")
    return TransitionRegime.CLEAN_HANDOFF


def _birth_quality_regime(
    pos: WavePosition,
    cfg: BehaviorConfig,
    reasons: list[str],
) -> BirthQualityRegime:
    """L3(v1.5)：candidate_wait_span, candidate_replacement_count, confirmation_distance_*。

    🔒 无 confirmation_distance_* → unknown_birth。
    """
    if pos.confirmation_distance_abs is None:
        # initial wave 或缺距离 → 无法刻画出生质量
        reasons.append("birth:unknown_birth(no_confirmation_distance)")
        return BirthQualityRegime.UNKNOWN_BIRTH
    rep = pos.candidate_replacement_count or 0
    if rep >= cfg.costly_birth_min_replacement:
        reasons.append(f"birth:costly_birth(rep={rep}>={cfg.costly_birth_min_replacement})")
        return BirthQualityRegime.COSTLY_BIRTH
    if rep >= cfg.negotiated_birth_min_replacement:
        reasons.append(f"birth:negotiated_birth(rep={rep}>={cfg.negotiated_birth_min_replacement})")
        return BirthQualityRegime.NEGOTIATED_BIRTH
    reasons.append("birth:clean_birth(rep=0)")
    return BirthQualityRegime.CLEAN_BIRTH
