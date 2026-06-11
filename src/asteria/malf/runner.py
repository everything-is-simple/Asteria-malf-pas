"""MALF 运行薄封装：load bars → CoreEngine.run。

供 UI / 脚本复用，避免上层直接拼装业务逻辑。只读，不写库（M1 阶段）。
"""

from __future__ import annotations

from datetime import date

from asteria.data import loader
from asteria.data.contracts import DailyBar
from asteria.malf.core import CoreEngine, CoreRunResult
from config import settings


def run_symbol(
    symbol: str,
    *,
    timeframe: str = "day",
    k: int = 2,
    start_dt: date | None = None,
    end_dt: date | None = None,
    price_line: str = settings.PRICE_LINE_STRUCTURE,
) -> tuple[list[DailyBar], CoreRunResult]:
    """加载单标的后复权日线并跑 Core 状态机。返回 (bars, result)。"""
    bars = loader.load_bars(
        symbol, price_line=price_line, start_dt=start_dt, end_dt=end_dt
    )
    eng = CoreEngine(symbol, timeframe=timeframe, k=k, source_run_id="ui-inspect")
    result = eng.run(bars)
    return bars, result
