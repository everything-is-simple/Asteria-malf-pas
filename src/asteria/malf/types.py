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
