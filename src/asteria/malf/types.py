"""MALF 结构事实的纯数据契约（v1.4 Core + v1.5 行为层）。

只定义形状与枚举，无副作用逻辑。所有比较前价格须先归一化精度（O3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


# --- 枚举 -----------------------------------------------------------------
class PivotKind(str, Enum):
    HIGH = "H"
    LOW = "L"


class Primitive(str, Enum):
    HH = "HH"
    HL = "HL"
    LL = "LL"
    LH = "LH"


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"


class SystemState(str, Enum):
    UNINITIALIZED = "uninitialized"
    UP_ALIVE = "up_alive"
    DOWN_ALIVE = "down_alive"
    TRANSITION = "transition"


class WaveCoreState(str, Enum):
    ALIVE = "alive"
    TERMINATED = "terminated"


# --- 结构对象 -------------------------------------------------------------
@dataclass
class Pivot:
    """已确认的局部极值点。

    extreme_bar_dt = 极值所在 bar；confirm_bar_dt = 确认所在 bar（延迟 k 根，O2）。
    price 属于 extreme bar；确认事件落在 confirm bar。
    """

    pivot_id: int
    kind: PivotKind
    extreme_bar_dt: date
    confirm_bar_dt: date
    price: float
    pivot_seq_in_bar: int = 0
    primitive: Primitive | None = None  # 相对前一同类极值判定 HH/HL/LL/LH


@dataclass
class Wave:
    """有向结构波（up/down）。"""

    wave_id: int
    direction: Direction
    start_bar_dt: date
    start_pivot_id: int | None
    wave_core_state: WaveCoreState = WaveCoreState.ALIVE
    end_bar_dt: date | None = None
    # 当前有效 guard：up wave 为最近确认 HL；down wave 为最近确认 LH。
    current_guard_pivot_id: int | None = None
    current_guard_price: float | None = None
    # progress_extreme：up wave 的最高 HH 价；down wave 的最低 LL 价。
    progress_extreme_pivot_id: int | None = None
    progress_extreme_price: float | None = None


@dataclass
class Break:
    """guard 被严格突破 → 旧波终止（D10，8 字段）。"""

    old_wave_id: int
    old_direction: Direction
    broken_guard_pivot_id: int | None
    broken_guard_price: float | None
    break_bar_dt: date
    break_price: float


@dataclass
class Transition:
    """旧波死亡后、新波确认前的未决边界态（D12/D13 双边界）。"""

    transition_id: int
    old_wave_id: int
    old_direction: Direction
    open_bar_dt: date
    boundary_high: float
    boundary_low: float
    resolved_bar_dt: date | None = None
    new_wave_id: int | None = None
    # transition 内的候选 guard（O4：latest 替换）
    active_candidate_pivot_id: int | None = None
    active_candidate_direction: Direction | None = None
    candidate_replacement_count: int = 0
    candidate_wait_span: int = 0


@dataclass
class CoreStateSnapshot:
    """逐 bar 的结构状态快照（O7 发布契约）。"""

    symbol: str
    timeframe: str
    bar_dt: date
    system_state: SystemState
    active_wave_id: int | None = None
    old_wave_id: int | None = None
    direction: Direction | None = None
    wave_core_state: WaveCoreState | None = None
    current_effective_guard_pivot_id: int | None = None
    current_effective_guard_price: float | None = None
    progress_extreme_pivot_id: int | None = None
    progress_extreme_price: float | None = None
    open_transition_id: int | None = None
    active_candidate_guard_pivot_id: int | None = None
    active_candidate_direction: Direction | None = None
    transition_boundary_high: float | None = None
    transition_boundary_low: float | None = None
    # 本 bar 是否发生 break / new wave 确认（供可视化与下游派生）
    break_event: Break | None = None
    new_wave_confirmed: bool = False


# =========================================================================
# L1b Lifespan：波段生命统计契约（MALF v1.4，M2 实现）
# =========================================================================
# 建立在 Core 已确认 wave 之上，只描述「生命统计位置」，不确认 wave 是否成立。
# 字段命名严格对齐 storage/schema.sql 的 wave_position 表与 PAS 设计 §2.3 消费需求。


class LifeState(str, Enum):
    """生命状态（设计 §3.3，判定顺序固定：terminal > stagnant > extended > early > developing）。"""

    EARLY = "early"
    DEVELOPING = "developing"
    EXTENDED = "extended"
    STAGNANT = "stagnant"
    TERMINAL = "terminal"


class PositionQuadrant(str, Enum):
    """update_rank × stagnation_rank 二维象限（设计 §3.4），不替代 life_state。"""

    EARLY_ACTIVE = "early_active"
    EARLY_STAGNANT = "early_stagnant"
    EXTENDED_ACTIVE = "extended_active"
    EXTENDED_STAGNANT = "extended_stagnant"
    DEVELOPING = "developing"


class BirthType(str, Enum):
    """波段出生类型（设计 §3.5 / L13）。"""

    INITIAL = "initial"
    SAME_DIRECTION_AFTER_BREAK = "same_direction_after_break"
    OPPOSITE_DIRECTION_AFTER_BREAK = "opposite_direction_after_break"


@dataclass
class WavePosition:
    """给 PAS 的主坐标（设计 §3.6 / L18）。逐 bar 一条。

    可空字段（rank/birth descriptors 等）在 transition 期 / uninitialized 期 / 样本不足时
    合法地为 None——字段级缺失 ≠ 错误，下游按缺失处理（PAS C1 null 规则）。
    """

    symbol: str
    timeframe: str
    bar_dt: date
    # --- 结构链路（来自 Core）---
    wave_id: int | None = None
    old_wave_id: int | None = None
    system_state: SystemState | None = None
    wave_core_state: WaveCoreState | None = None
    # transition 期 direction = old_direction（L-T5）
    direction: Direction | None = None
    # --- 计数（§3.1）---
    new_count: int = 0
    no_new_span: int = 0
    transition_span: int = 0
    # --- rank 与派生状态（§3.2/§3.3/§3.4）---
    update_rank: float | None = None
    stagnation_rank: float | None = None
    life_state: LifeState | None = None
    position_quadrant: PositionQuadrant | None = None
    # --- birth descriptors（§3.5）---
    birth_type: BirthType | None = None
    candidate_wait_span: int | None = None
    candidate_replacement_count: int | None = None
    confirmation_distance_abs: float | None = None
    confirmation_distance_pct: float | None = None
    # --- 版本字段 ---
    sample_version: str | None = None
    lifespan_rule_version: str | None = None
    source_run_id: str | None = None


# =========================================================================
# L1c v1.5 行为层：六个 regime 契约（纯派生，M2 实现）
# =========================================================================
# 只能从 v1.4 已确认结构事实派生，不得引入强弱评分。
# 字段命名严格对齐 storage/schema.sql 的 wave_behavior_snapshot 表与 PAS 设计 §4.2 来源字段。


class ContinuationRegime(str, Enum):
    """延续 regime（C1）：来源 system_state, new_count, no_new_span, rank。"""

    ADVANCING = "advancing"
    SLOWING = "slowing"
    STALLED = "stalled"
    TRANSITIONING = "transitioning"


class BoundaryPressureRegime(str, Enum):
    """边界压力 regime（C2）：来源与 guard/boundary 的关系。"""

    CONTINUATION_SIDE = "continuation_side"
    GUARD_PRESSURE = "guard_pressure"
    TRANSITION_PRESSURE = "transition_pressure"
    NEUTRAL = "neutral"


class DirectionalContinuityRegime(str, Enum):
    """方向连续性 regime（C3）：来源 direction, old_direction, birth_type, system_state。"""

    SAME_DIRECTION_CONTINUATION = "same_direction_continuation"
    OPPOSITE_DIRECTION_REBIRTH = "opposite_direction_rebirth"
    TRANSITION_UNRESOLVED = "transition_unresolved"


class StagnationRegime(str, Enum):
    """停滞 regime（L1 v1.5）：来源 no_new_span, stagnation_rank, life_state。"""

    FRESH = "fresh"
    WATCHFUL = "watchful"
    STALLED = "stalled"
    TERMINAL_PRESSURE = "terminal_pressure"


class TransitionRegime(str, Enum):
    """转换 regime（L2 v1.5）：来源 transition_span, candidate_replacement_count, open_transition_id。"""

    CLEAN_HANDOFF = "clean_handoff"
    REPLACEMENT_HEAVY = "replacement_heavy"
    PROLONGED_UNRESOLVED = "prolonged_unresolved"
    NOT_APPLICABLE = "not_applicable"


class BirthQualityRegime(str, Enum):
    """出生质量 regime（L3 v1.5）：来源 candidate_wait_span, candidate_replacement_count, confirmation_distance_*。"""

    CLEAN_BIRTH = "clean_birth"
    NEGOTIATED_BIRTH = "negotiated_birth"
    COSTLY_BIRTH = "costly_birth"
    UNKNOWN_BIRTH = "unknown_birth"


@dataclass
class WaveBehaviorSnapshot:
    """v1.5 主接口（设计 §4.3 / MALF_03）。逐 bar 一条，只读·非决策。

    🔒 Service 铁律：本对象 != strength score / setup family / accept-reject /
    order / position / fill / profit。永不被 PAS 或下游写回。
    """

    # --- identity ---
    symbol: str
    timeframe: str
    bar_dt: date
    # --- wave linkage ---
    wave_id: int | None = None
    direction: Direction | None = None
    old_wave_id: int | None = None
    open_transition_id: int | None = None
    # --- 六个 regime ---
    continuation_regime: ContinuationRegime | None = None
    directional_continuity_regime: DirectionalContinuityRegime | None = None
    stagnation_regime: StagnationRegime | None = None
    boundary_pressure_regime: BoundaryPressureRegime | None = None
    transition_regime: TransitionRegime | None = None
    birth_quality_regime: BirthQualityRegime | None = None
    # --- audit / lineage / version ---
    reason_codes: list[str] = field(default_factory=list)
    lineage_hash: str | None = None
    malf_v1_4_rule_version: str | None = None
    malf_v1_5_rule_version: str | None = None
    source_run_id: str | None = None
