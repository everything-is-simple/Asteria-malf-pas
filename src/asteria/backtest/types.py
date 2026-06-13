"""回测层纯数据契约（docs/02-module-design/BACKTEST_DESIGN.md §6）。

🔒 边界铁律：回测层是唯一拥有仓位/订单/成交/盈亏语义的层。
上游 MALF/PAS/Signal 都不碰这些结构。

只定义形状与枚举，无副作用逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from asteria.malf.types import Direction


# =========================================================================
# 枚举
# =========================================================================
class Side(str, Enum):
    """订单方向。"""

    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """订单类型（MVP 只做集合竞价 market-on-open）。"""

    MOO = "moo"  # market-on-open 集合竞价


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    REJECTED = "rejected"


class OrderReason(str, Enum):
    """下单缘由（审计 + 平仓归因）。"""

    ENTRY = "entry"
    STOP = "stop"
    TARGET1 = "target1"
    TARGET2 = "target2"  # 第二目标（结构量度移动）
    TRAILING = "trailing"
    TIME_STOP = "time_stop"
    BREAKDOWN = "breakdown"  # 买入日破线


class FillRejectReason(str, Enum):
    """撮合拒绝原因（涨跌停/停牌）。"""

    NONE = "none"
    LIMIT_UP = "limit_up"
    LIMIT_DOWN = "limit_down"
    HALT = "halt"


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class ExitReason(str, Enum):
    """平仓归因（写入 bt_trade.exit_reason）。"""

    STOP = "stop"
    TARGET1 = "target1"
    TARGET2 = "target2"  # 第二目标（结构量度移动）
    TRAILING = "trailing"
    TIME_STOP = "time_stop"
    BREAKDOWN = "breakdown"


# =========================================================================
# 数据结构
# =========================================================================
@dataclass(frozen=True)
class StructuralLevels:
    """单 bar 的 MALF 结构价只读投影（来自 CoreStateSnapshot）。

    只带 Signal/rules 需要的价，不泄漏 transition/candidate 等 MALF 内部字段。
    progress_extreme = up-wave 已创最高 HH（前高/最近阻力，T1 与 RR 的结构锚）；
    guard = 最近确认 HL（保护性 swing low，结构跟踪台阶）。
    逐 bar 一条，与 PASCoreSnapshot/qfq_bars 1:1 对应。
    """

    bar_dt: date
    progress_extreme_price: float | None = None
    guard_price: float | None = None
    direction: Direction | None = None
    system_state: str | None = None  # CoreStateSnapshot.system_state.value（审计）
    life_state: str | None = None  # WavePosition.life_state.value（质量门 life_state 上限用）


@dataclass
class Order:
    """挂单意图（T0 收盘后生成，T1 集合竞价撮合）。"""

    order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    intended_dt: date  # 拟成交日（T+1）
    reason: OrderReason
    qty: float
    status: OrderStatus = OrderStatus.PENDING
    limit_ref: float | None = None  # 计划参考价（审计）
    # 进场单专用：携带候选 + T0.low 供进场后重算权威止损
    signal_candidate_key: str | None = None
    t0_low: float | None = None
    # 进场单专用：发现时捕获的 MALF 结构价，建仓时算权威 T1/T2（与 t0_low 同款）
    struct_progress_extreme: float | None = None
    struct_guard: float | None = None
    # 平仓单专用：关联仓位
    position_id: str | None = None


@dataclass
class Fill:
    """成交回报。"""

    fill_id: str
    order_id: str
    symbol: str
    fill_dt: date
    fill_price: float  # 后复权（qfq_back）成交价，用于盈亏
    qty: float
    reject_reason: FillRejectReason = FillRejectReason.NONE

    @property
    def filled(self) -> bool:
        return self.reject_reason == FillRejectReason.NONE


@dataclass
class Position:
    """持仓（建仓到平仓的生命周期）。"""

    position_id: str
    symbol: str
    direction: Direction
    entry_dt: date
    entry_price: float
    qty: float  # 当前剩余仓位
    original_qty: float  # 进场原始仓位（R_multiple 用）
    initial_stop: float
    risk_unit_R: float  # entry - stop（按实际成交价重算，决策 3）
    target1: float
    current_stop: float  # 移动止损（不断上移）
    target2: float | None = None  # 第二目标（结构量度移动，D2）；None = 纯跟踪兜底
    breakeven_armed: bool = False  # 达 T1 后已拉保本（D1）
    half_exited: bool = False
    bars_held: int = 0
    bars_since_new_extreme: int = 0  # 自进场后无新高累计（时间止损，决策 1）
    extreme_since_entry: float | None = None  # 自进场以来最高价
    realized_pnl: float = 0.0  # 部分平仓已实现（减仓 50%）
    status: PositionStatus = PositionStatus.OPEN
    signal_candidate_key: str | None = None


@dataclass
class Trade:
    """平仓后结算的一笔完整交易（减仓后合并为一行混合，决策 4）。"""

    trade_id: str
    symbol: str
    entry_dt: date
    exit_dt: date
    entry_price: float
    avg_exit_price: float  # 加权平均出场价（含减仓）
    qty: float  # 原始进场仓位
    realized_pnl: float
    R_multiple: float  # realized_pnl / (risk_unit_R × original_qty)
    exit_reason: ExitReason
    signal_candidate_key: str | None = None  # 引擎内部键，writer 解析成 db id


@dataclass
class EquityPoint:
    """逐 bar 组合净值快照。"""

    bar_dt: date
    equity: float
    cash: float
    open_positions: int
