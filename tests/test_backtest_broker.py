"""A 股撮合测试（M4 §4）。

覆盖：各 board 涨跌停限价 + 涨停拒买 + 跌停拒卖 + 集合竞价 open 成交 + 停牌。
"""

from datetime import date

from asteria.backtest.broker import BrokerConfig, price_limits, try_fill
from asteria.backtest.types import (
    FillRejectReason,
    Order,
    OrderReason,
    OrderType,
    Side,
)


def make_order(side: Side, qty: float = 100.0) -> Order:
    return Order(
        order_id="o1",
        symbol="600000.SH",
        side=side,
        order_type=OrderType.MOO,
        intended_dt=date(2025, 1, 10),
        reason=OrderReason.ENTRY if side == Side.BUY else OrderReason.STOP,
        qty=qty,
    )


# =========================================================================
# 涨跌停限价（不复权前收盘价）
# =========================================================================
def test_price_limits_main_board():
    """主板 ±10%：prev=10.00 → up=11.00 / down=9.00。"""
    up, down = price_limits(10.0, board="main")
    assert up == 11.0
    assert down == 9.0


def test_price_limits_chinext_star():
    """创业板/科创板 ±20%：prev=10.00 → 12.00 / 8.00。"""
    for board in ("chinext", "star"):
        up, down = price_limits(10.0, board=board)
        assert up == 12.0
        assert down == 8.0


def test_price_limits_bse():
    """北交所 ±30%：prev=10.00 → 13.00 / 7.00。"""
    up, down = price_limits(10.0, board="bse")
    assert up == 13.0
    assert down == 7.0


def test_price_limits_st():
    """ST ±5%（优先于 board）：prev=10.00 → 10.50 / 9.50。"""
    up, down = price_limits(10.0, board="main", is_st=True)
    assert up == 10.5
    assert down == 9.5


def test_price_limits_rounding():
    """round 到 2 位：prev=9.99 主板 → up=10.99 / down=8.99。"""
    up, down = price_limits(9.99, board="main")
    assert up == 10.99
    assert down == 8.99


# =========================================================================
# 撮合
# =========================================================================
def test_call_auction_fills_at_open():
    """正常成交价 = bar open（qfq）。"""
    order = make_order(Side.BUY)
    fill = try_fill(order, open_qfq=10.5, open_raw=10.5, up_limit=11.0, down_limit=9.0)
    assert fill.filled
    assert fill.fill_price == 10.5
    assert fill.qty == 100.0
    assert fill.reject_reason == FillRejectReason.NONE


def test_buy_rejected_on_limit_up():
    """一字涨停（open_raw ≥ up_limit）→ 拒买 LIMIT_UP。"""
    order = make_order(Side.BUY)
    fill = try_fill(order, open_qfq=11.0, open_raw=11.0, up_limit=11.0, down_limit=9.0)
    assert not fill.filled
    assert fill.qty == 0.0
    assert fill.reject_reason == FillRejectReason.LIMIT_UP


def test_sell_rejected_on_limit_down():
    """一字跌停（open_raw ≤ down_limit）→ 拒卖 LIMIT_DOWN。"""
    order = make_order(Side.SELL)
    fill = try_fill(order, open_qfq=9.0, open_raw=9.0, up_limit=11.0, down_limit=9.0)
    assert not fill.filled
    assert fill.reject_reason == FillRejectReason.LIMIT_DOWN


def test_buy_ok_below_limit_up():
    """开盘略低于涨停价 → 可成交。"""
    order = make_order(Side.BUY)
    fill = try_fill(order, open_qfq=10.99, open_raw=10.99, up_limit=11.0, down_limit=9.0)
    assert fill.filled


def test_sell_ok_above_limit_down():
    order = make_order(Side.SELL)
    fill = try_fill(order, open_qfq=9.01, open_raw=9.01, up_limit=11.0, down_limit=9.0)
    assert fill.filled


def test_halt_rejects():
    """停牌 → HALT。"""
    order = make_order(Side.BUY)
    fill = try_fill(
        order, open_qfq=10.0, open_raw=10.0, up_limit=11.0, down_limit=9.0, halted=True
    )
    assert not fill.filled
    assert fill.reject_reason == FillRejectReason.HALT


def test_dual_track_raw_for_limit_qfq_for_fill():
    """复权双轨：限价比较用 open_raw，成交价用 open_qfq。

    后复权 open 高，但不复权 open 已涨停 → 应拒（用 raw 判定）。
    """
    order = make_order(Side.BUY)
    # raw 开盘=涨停价 → 拒；即使 qfq 价不同
    fill = try_fill(order, open_qfq=22.0, open_raw=11.0, up_limit=11.0, down_limit=9.0)
    assert not fill.filled
    assert fill.reject_reason == FillRejectReason.LIMIT_UP


def test_broker_config_custom_pct():
    """自定义涨跌停比例。"""
    cfg = BrokerConfig(default_price_limit_pct=0.15)
    up, down = price_limits(10.0, board="nonexistent_board", cfg=cfg)
    assert up == 11.5
    assert down == 8.5
