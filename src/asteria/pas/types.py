"""PAS v1.5 纯数据契约（PAS_01B + PAS_02）。

只定义形状与枚举，无副作用逻辑。

🔒 边界铁律：
- PAS 禁读 PriceBar、禁重算 MALF。
- 输入只有 WavePosition + WaveBehaviorSnapshot。
- 输出只有 posture（5 族 × 4 档），不做 accept/reject。
- 内部三态树标签不发布到 PASCoreSnapshot。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from asteria.malf.types import Direction


# =========================================================================
# 内部三态树枚举（IA-1~4，不发布）
# =========================================================================
class _InternalTriState(str, Enum):
    """内部三态标签（IA-1/IA-2），不写入 PASCoreSnapshot。"""

    DENYING = "denying"
    PROVING = "proving"
    NEUTRAL = "neutral"


class _NeutralSubtype(str, Enum):
    """Neutral 五子类（IA-4），按编号排序取最小。"""

    TERMINAL_OBSERVATION = "terminal_observation"
    STAGNANT = "stagnant"
    SLOWING = "slowing"
    NEWBORN = "newborn"
    WATCHFUL = "watchful"


# =========================================================================
# DirectionalPremise（IA-5 映射产物，发布）
# =========================================================================
class DirectionalPremise(str, Enum):
    """方向假设（IA-5）。"""

    EXPECT_STRENGTH_CONTINUATION = "expect_strength_continuation"
    EXPECT_WEAKNESS_REJECTION = "expect_weakness_rejection"
    EXPECT_BOUNDARY_TEST = "expect_boundary_test"
    EXPECT_TRANSITION_RESOLUTION = "expect_transition_resolution"
    NO_ACTIONABLE_PREMISE = "no_actionable_premise"
    NOT_APPLICABLE = "not_applicable"


# =========================================================================
# ReadStatus（从 EvidenceTriplet 派生，发布）
# =========================================================================
class ReadStatus(str, Enum):
    """证据强度（由 strength/weakness/ambiguity 计数派生）。"""

    STRONG = "strong"
    WEAK = "weak"
    MIXED = "mixed"
    AMBIGUOUS = "ambiguous"
    NOT_APPLICABLE = "not_applicable"


# =========================================================================
# Posture（PM-T1~6 查表结果，发布）
# =========================================================================
class Posture(str, Enum):
    """单族 setup 适用性档位（4 档）。"""

    FAVORED = "favored"
    ALLOWED = "allowed"
    DEFERRED = "deferred"
    BLOCKED = "blocked"


# =========================================================================
# 五个 Setup Family（PM 输出列）
# =========================================================================
class SetupFamily(str, Enum):
    """五个 setup 族（列名简写）。"""

    TST = "TST"  # Trend Structure Test
    BOF = "BOF"  # Break of Form
    BPB = "BPB"  # Break & Pullback
    PB = "PB"  # Pullback
    CPB = "CPB"  # Counter-Pullback


# =========================================================================
# PAS Core Snapshot（发布契约）
# =========================================================================
@dataclass
class PASCoreSnapshot:
    """PAS Core 快照（发布契约）。

    🔒 禁止字段：三态标签、数值分数、accept/reject/buy/sell/order/position/fill/profit。
    """

    # --- identity ---
    symbol: str
    timeframe: str
    bar_dt: date
    # --- MALF linkage ---
    wave_id: int | None = None
    direction: Direction | None = None
    system_state: str | None = None  # 存 MALF SystemState.value，避免循环依赖
    # --- Premise + ReadStatus ---
    directional_premise: DirectionalPremise | None = None
    read_status: ReadStatus | None = None
    # --- Posture Matrix（5 族 × 4 档）---
    tst_posture: Posture | None = None
    bof_posture: Posture | None = None
    bpb_posture: Posture | None = None
    pb_posture: Posture | None = None
    cpb_posture: Posture | None = None
    # --- C6 上限约束触发标记（audit）---
    transition_bound: bool = False
    lineage_gap: bool = False
    ambiguity_dominates: bool = False
    # --- EvidenceTriplet counts（audit）---
    strength_evidence_count: int = 0
    weakness_evidence_count: int = 0
    ambiguity_evidence_count: int = 0
    # --- audit / lineage ---
    reason_codes: list[str] = field(default_factory=list)
    lineage_hash: str | None = None
    pas_core_rule_version: str | None = None
    source_run_id: str | None = None


# =========================================================================
# PAS Lifespan（四态机）
# =========================================================================
class LifespanState(str, Enum):
    """PAS Lifespan 四态（L-TR1~5）。"""

    OBSERVING = "observing"
    ACTIVE = "active"
    SUBMITTED = "submitted"
    INVALIDATED = "invalidated"


@dataclass
class PASLifespanRecord:
    """PAS Lifespan 记录（逐 bar 一条）。"""

    # --- identity ---
    symbol: str
    timeframe: str
    bar_dt: date
    lifespan_id: str  # UUID 或自增 ID
    # --- state ---
    lifespan_state: LifespanState
    # --- transition triggers ---
    transition_reason: str | None = None
    # --- linkage ---
    wave_id: int | None = None
    pas_snapshot_bar_dt: date | None = None  # 关联的 PASCoreSnapshot bar_dt
    # --- audit ---
    source_run_id: str | None = None


# =========================================================================
# PAS Service 发布接口（PAS_04）
# =========================================================================
@dataclass
class PASCandidateRecord:
    """候选机会记录（Latest）。"""

    symbol: str
    timeframe: str
    bar_dt: date
    wave_id: int | None = None
    directional_premise: DirectionalPremise | None = None
    read_status: ReadStatus | None = None
    # 5 族 posture
    tst_posture: Posture | None = None
    bof_posture: Posture | None = None
    bpb_posture: Posture | None = None
    pb_posture: Posture | None = None
    cpb_posture: Posture | None = None
    # 只读发布，不含 accept/reject 决策
    source_run_id: str | None = None


@dataclass
class PASServiceHandoffRecord:
    """PAS → Signal 切换记录（L-TR2）。"""

    symbol: str
    timeframe: str
    bar_dt: date
    lifespan_id: str
    handoff_dt: date  # Signal 接收时间
    wave_id: int | None = None
    source_run_id: str | None = None


@dataclass
class SignalFeedback:
    """Signal → PAS 反馈（PAS_04，只用于 audit/统计/replay，绝不回写）。"""

    symbol: str
    timeframe: str
    bar_dt: date
    lifespan_id: str
    signal_decision: str  # "accepted" | "rejected"
    decision_reason: str | None = None
    signal_rule_version: str | None = None
    source_run_id: str | None = None
