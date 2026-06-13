"""绩效指标汇总（docs/02-module-design/BACKTEST_DESIGN.md §8 §10）。

纯函数，无 I/O。输入逐笔 Trade + 逐 bar 权益曲线，输出与 bt_metrics 列 1:1 的 BtMetrics。

指标定义：
  total_return  = equity[-1] / initial_capital − 1
  cagr          = (equity[-1]/initial_capital)^(periods_per_year/n_bars) − 1
  max_drawdown  = max 峰谷回撤（正数表示回撤幅度）
  sharpe        = mean(日收益)/std(日收益) × √periods_per_year（无风险利率取 0）
  win_rate      = 盈利交易数 / 总交易数
  avg_R         = mean(R_multiple)
  expectancy    = mean(realized_pnl)
  profit_factor = 毛盈 / 毛亏（毛亏为 0 → None/∞ 用 None 表示）
  trade_count   = 交易笔数

空交易/空曲线返回 0 或 None（不报错）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from asteria.backtest.types import EquityPoint, Trade


@dataclass
class BtMetrics:
    """回测汇总指标（对齐 schema bt_metrics 列 1:1）。"""

    total_return: float | None = None
    cagr: float | None = None
    max_drawdown: float | None = None
    sharpe: float | None = None
    win_rate: float | None = None
    avg_R: float | None = None
    expectancy: float | None = None
    trade_count: int = 0
    profit_factor: float | None = None


def _max_drawdown(equities: list[float]) -> float:
    """峰谷最大回撤（返回正数，0 表示无回撤）。"""
    if not equities:
        return 0.0
    peak = equities[0]
    max_dd = 0.0
    for e in equities:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _sharpe(equities: list[float], periods_per_year: int) -> float | None:
    """年化夏普（无风险利率 0）。样本不足返回 None。"""
    if len(equities) < 2:
        return None
    rets: list[float] = []
    for prev, cur in zip(equities[:-1], equities[1:]):
        if prev > 0:
            rets.append(cur / prev - 1.0)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    return (mean / std) * math.sqrt(periods_per_year)


def compute_metrics(
    trades: list[Trade],
    equity_curve: list[EquityPoint],
    *,
    initial_capital: float,
    periods_per_year: int = 252,
) -> BtMetrics:
    """汇总绩效指标。空交易/空曲线安全返回 0/None。"""
    n = len(trades)
    if n == 0 and not equity_curve:
        return BtMetrics(trade_count=0)

    equities = [p.equity for p in equity_curve]

    # --- 权益类指标 ---
    total_return: float | None = None
    cagr: float | None = None
    max_dd: float | None = None
    sharpe: float | None = None
    if equities and initial_capital > 0:
        total_return = equities[-1] / initial_capital - 1.0
        max_dd = _max_drawdown(equities)
        sharpe = _sharpe(equities, periods_per_year)
        n_bars = len(equities)
        if n_bars > 1 and equities[-1] > 0:
            cagr = (equities[-1] / initial_capital) ** (periods_per_year / n_bars) - 1.0

    # --- 交易类指标 ---
    win_rate: float | None = None
    avg_R: float | None = None
    expectancy: float | None = None
    profit_factor: float | None = None
    if n > 0:
        wins = [t for t in trades if t.realized_pnl > 0]
        win_rate = len(wins) / n
        avg_R = sum(t.R_multiple for t in trades) / n
        expectancy = sum(t.realized_pnl for t in trades) / n
        gross_win = sum(t.realized_pnl for t in trades if t.realized_pnl > 0)
        gross_loss = -sum(t.realized_pnl for t in trades if t.realized_pnl < 0)
        profit_factor = gross_win / gross_loss if gross_loss > 0 else None

    return BtMetrics(
        total_return=total_return,
        cagr=cagr,
        max_drawdown=max_dd,
        sharpe=sharpe,
        win_rate=win_rate,
        avg_R=avg_R,
        expectancy=expectancy,
        trade_count=n,
        profit_factor=profit_factor,
    )
