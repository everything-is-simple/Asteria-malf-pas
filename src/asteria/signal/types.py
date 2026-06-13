"""Signal 层纯数据契约（docs/02-module-design/BACKTEST_DESIGN.md §3 §6）。

只定义形状与枚举，无副作用逻辑。

🔒 边界铁律：
- Signal 只读 PAS posture（PASCoreSnapshot）+ 风报比规则做 accept/reject。
- 输出 SignalCandidate（计划值），不回写 PAS/MALF（PAS C-T3）。
- 无仓位/订单/成交/盈亏字段——那是回测层的语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from asteria.pas.types import DirectionalPremise, Posture, ReadStatus, SetupFamily

SIGNAL_RULE_VERSION = "signal-v1.0-mvp"


# =========================================================================
# 决策枚举
# =========================================================================
class SignalDecision(str, Enum):
    """accept/reject（对齐 schema signal_candidate.decision）。"""

    ACCEPT = "accept"
    REJECT = "reject"


class RejectReason(str, Enum):
    """拒绝原因（§3 分支 + [signal] accept_families 过滤 + PAS 质量门 D4）。"""

    FAMILY_NOT_ACCEPTED = "family_not_accepted"
    POSTURE_NOT_ALLOWED = "posture_not_allowed"
    READ_STATUS_TOO_WEAK = "read_status_too_weak"  # 质量门基本条件：信号强度不足
    LIFE_STATE_EXHAUSTED = "life_state_exhausted"  # 质量门 life_state 上限：末端/衰竭波
    QUALITY_BELOW_MIN = "quality_below_min"  # 质量门 2+N 评分不达标
    AMBIGUITY_DOMINATES = "ambiguity_dominates"  # 仅 veto_ambiguity_dominates=True 时触发
    RR_BELOW_MIN = "rr_below_min"
    NOT_TRADABLE = "not_tradable"


# =========================================================================
# Signal 配置（来自 config/params_default.toml [signal] + [backtest]）
# =========================================================================
@dataclass(frozen=True)
class SignalConfig:
    """Signal accept/reject 规则参数。

    D3：RR 对结构前高算，min_reward_risk=1.5（惰性 RR 路径已消除）。
    D4：PAS 质量门（许佳冲「2+N」）——基本条件 read_status ∈ accepted_read_status，
        评分满分 5（favored/strong/premise可操作/证据偏强/无C6旗标），门槛 min_quality_score。
    """

    accepted_postures: frozenset[Posture] = field(
        default_factory=lambda: frozenset({Posture.FAVORED, Posture.ALLOWED})
    )
    accept_families: frozenset[SetupFamily] = field(
        default_factory=lambda: frozenset(SetupFamily)
    )
    # --- D4 质量门 ---
    accepted_read_status: frozenset[ReadStatus] = field(
        default_factory=lambda: frozenset({ReadStatus.STRONG, ReadStatus.MIXED})
    )
    actionable_premises: frozenset[DirectionalPremise] = field(
        default_factory=lambda: frozenset(
            {
                DirectionalPremise.EXPECT_STRENGTH_CONTINUATION,
                DirectionalPremise.EXPECT_BOUNDARY_TEST,
                DirectionalPremise.EXPECT_TRANSITION_RESOLUTION,
            }
        )
    )
    min_quality_score: int = 2
    veto_ambiguity_dominates: bool = False
    # life_state 上限（修复2）：挡掉衰竭/末端波（terminal/stagnant），只做仍有上行潜力的
    # early/developing/extended。用字符串（WavePosition.life_state.value），signal 不耦合 MALF 枚举。
    # None=不启用 life_state 门（向后兼容直接调用方）。
    accepted_life_states: frozenset[str] | None = field(
        default_factory=lambda: frozenset({"early", "developing", "extended"})
    )
    # --- D3 风报比 ---
    min_reward_risk: float = 1.5
    target_ref: str = "structural"  # 废弃字段（RR 改对结构前高算，保留兼容）
    stop_offset: float = 0.02
    target_r: float = 1.0
    # 最小风险距离占 entry 比例（修复 RR 虚高）：T0 收盘贴最低时 1R 趋近 stop_offset，
    # floor 到 min_risk_pct×entry，让 stop/1R/RR/仓位用同一诚实风险值。0=不启用。
    min_risk_pct: float = 0.03
    signal_rule_version: str = SIGNAL_RULE_VERSION


DEFAULT_CONFIG = SignalConfig()


# =========================================================================
# SignalCandidate（发布契约，对齐 schema signal_candidate 列 1:1）
# =========================================================================
@dataclass
class SignalCandidate:
    """单个机会的裁决结果（含进场/止损/目标计划值）。

    计划值（planned_*）基于预期开盘价算，供审计入库；引擎按 T+1 实际成交价重算
    权威 stop/1R/target1（决策 3）。
    """

    # --- identity ---
    symbol: str
    timeframe: str
    discover_dt: date  # T0 机会发现日
    # --- PAS linkage ---
    setup_family: SetupFamily
    directional_premise: DirectionalPremise | None = None
    read_status: ReadStatus | None = None
    # --- 计划值（基于预期开盘）---
    planned_entry: float | None = None
    planned_stop: float | None = None
    planned_target1: float | None = None
    planned_target2: float | None = None  # 第二目标（结构量度移动，D2）
    reward_risk: float | None = None
    # --- 决策 ---
    decision: SignalDecision = SignalDecision.REJECT
    reason: str | None = None
    # --- audit / lineage ---
    lifespan_id: str | None = None
    signal_rule_version: str = SIGNAL_RULE_VERSION
    source_run_id: str | None = None
