"""绩效指标测试（M4 §8）。oracle 手算。"""

from datetime import date, timedelta

from asteria.backtest.metrics import compute_metrics
from asteria.backtest.types import EquityPoint, ExitReason, Trade

_D0 = date(2025, 1, 10)


def _trade(pnl: float, r: float) -> Trade:
    return Trade(
        trade_id="t",
        symbol="600000.SH",
        entry_dt=_D0,
        exit_dt=_D0 + timedelta(days=5),
        entry_price=10.0,
        avg_exit_price=10.0 + pnl / 1000.0,
        qty=1000.0,
        realized_pnl=pnl,
        R_multiple=r,
        exit_reason=ExitReason.TARGET1,
    )


def _equity(values: list[float]) -> list[EquityPoint]:
    return [
        EquityPoint(bar_dt=_D0 + timedelta(days=i), equity=v, cash=v, open_positions=0)
        for i, v in enumerate(values)
    ]


def test_empty_trades_safe():
    m = compute_metrics([], [], initial_capital=1_000_000.0)
    assert m.trade_count == 0
    assert m.win_rate is None


def test_win_rate_and_counts():
    """3 盈 1 亏 → win_rate=0.75。"""
    trades = [_trade(100, 1.0), _trade(200, 2.0), _trade(50, 0.5), _trade(-100, -1.0)]
    m = compute_metrics(trades, _equity([1e6, 1e6]), initial_capital=1_000_000.0)
    assert m.trade_count == 4
    assert m.win_rate == 0.75


def test_avg_r_and_expectancy():
    """avg_R = mean(R)；expectancy = mean(pnl)。"""
    trades = [_trade(100, 1.0), _trade(-100, -1.0), _trade(300, 3.0)]
    m = compute_metrics(trades, _equity([1e6, 1e6]), initial_capital=1_000_000.0)
    # R: (1 - 1 + 3)/3 = 1.0
    assert abs(m.avg_R - 1.0) < 1e-9
    # pnl: (100 - 100 + 300)/3 = 100
    assert abs(m.expectancy - 100.0) < 1e-9


def test_profit_factor():
    """毛盈 400 / 毛亏 100 = 4.0。"""
    trades = [_trade(300, 3.0), _trade(100, 1.0), _trade(-100, -1.0)]
    m = compute_metrics(trades, _equity([1e6, 1e6]), initial_capital=1_000_000.0)
    assert abs(m.profit_factor - 4.0) < 1e-9


def test_profit_factor_none_when_no_loss():
    """无亏损交易 → profit_factor=None（避免除零）。"""
    trades = [_trade(100, 1.0), _trade(200, 2.0)]
    m = compute_metrics(trades, _equity([1e6, 1e6]), initial_capital=1_000_000.0)
    assert m.profit_factor is None


def test_total_return_and_max_drawdown():
    """equity 1.0M→1.2M→0.9M→1.1M。

    total_return = 1.1/1.0 - 1 = 0.10
    max_dd：峰 1.2M → 谷 0.9M → (1.2-0.9)/1.2 = 0.25
    """
    m = compute_metrics(
        [_trade(100, 1.0)],
        _equity([1_000_000, 1_200_000, 900_000, 1_100_000]),
        initial_capital=1_000_000.0,
    )
    assert abs(m.total_return - 0.10) < 1e-9
    assert abs(m.max_drawdown - 0.25) < 1e-9
