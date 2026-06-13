"""A 股撮合（docs/02-module-design/BACKTEST_DESIGN.md §4 §2 规则 9 §5 步1）。

MVP 集合竞价撮合：成交价 = T1 bar open。
- 盈亏用后复权（qfq_back）成交价；
- 涨跌停限价用不复权（raw_none）前收盘价另算（复权双轨，§4）。

涨跌停（撮合约束）：
- 买入：open_raw ≥ up_limit → 拒成交 LIMIT_UP（一字涨停无法买入）
- 卖出：open_raw ≤ down_limit → 拒成交 LIMIT_DOWN（一字跌停无法卖出）

T+1（买入次日才可卖）在引擎层强制（步3 不卖当日进场仓位），不在 broker。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from asteria.backtest.types import Fill, FillRejectReason, Order, Side
from asteria.data.universe import LIMIT_PCT_BY_BOARD, ST_LIMIT_PCT


@dataclass(frozen=True)
class BrokerConfig:
    """撮合参数（涨跌停比例按 board）。"""

    default_price_limit_pct: float = 0.10
    limit_pct_by_board: dict[str, float] = field(
        default_factory=lambda: dict(LIMIT_PCT_BY_BOARD)
    )
    st_limit_pct: float = ST_LIMIT_PCT


DEFAULT_CONFIG = BrokerConfig()


def _round2(x: float) -> float:
    return round(x, 2)


def price_limits(
    prev_close_raw: float,
    *,
    board: str,
    is_st: bool = False,
    cfg: BrokerConfig = DEFAULT_CONFIG,
) -> tuple[float, float]:
    """返回 (涨停价, 跌停价)。用不复权前收盘价（raw_none）算。

    limit = round(prev_close × (1 ± pct), 2)。
    """
    pct = cfg.st_limit_pct if is_st else cfg.limit_pct_by_board.get(
        board, cfg.default_price_limit_pct
    )
    up = _round2(prev_close_raw * (1.0 + pct))
    down = _round2(prev_close_raw * (1.0 - pct))
    return up, down


def try_fill(
    order: Order,
    *,
    open_qfq: float,
    open_raw: float,
    up_limit: float,
    down_limit: float,
    halted: bool = False,
) -> Fill:
    """对一笔挂单尝试撮合。成交价 = bar open（集合竞价 MVP）。

    open_qfq：后复权开盘价（成交价，算盈亏）。
    open_raw：不复权开盘价（与涨跌停限价比较）。
    """
    fill_id = f"f-{order.order_id}"

    def _reject(reason: FillRejectReason) -> Fill:
        return Fill(
            fill_id=fill_id,
            order_id=order.order_id,
            symbol=order.symbol,
            fill_dt=order.intended_dt,
            fill_price=open_qfq,
            qty=0.0,
            reject_reason=reason,
        )

    if halted:
        return _reject(FillRejectReason.HALT)

    # 涨跌停撮合约束（用不复权价比较）
    if order.side == Side.BUY and open_raw >= up_limit:
        return _reject(FillRejectReason.LIMIT_UP)
    if order.side == Side.SELL and open_raw <= down_limit:
        return _reject(FillRejectReason.LIMIT_DOWN)

    return Fill(
        fill_id=fill_id,
        order_id=order.order_id,
        symbol=order.symbol,
        fill_dt=order.intended_dt,
        fill_price=open_qfq,
        qty=order.qty,
        reject_reason=FillRejectReason.NONE,
    )
