"""MALF 运行薄封装：load bars → Core → Lifespan → v1.5 行为快照。

供 UI / 脚本复用，避免上层直接拼装业务逻辑。

分两层：
- ``assemble_from_bars``：纯内存组装（Core → lifespan → behavior），不碰库，可单测。
- ``run_symbol_full``：先 load 行情，再调 ``assemble_from_bars``。
- ``run_symbol``：M1 兼容入口，只跑 Core（保留给现有 UI 调用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from asteria.data import loader
from asteria.data.contracts import DailyBar
from asteria.malf.behavior import BehaviorConfig, derive_behavior_snapshots
from asteria.malf.core import CoreEngine, CoreRunResult
from asteria.malf.lifespan import LifespanConfig, compute_wave_positions
from asteria.malf.types import WaveBehaviorSnapshot, WavePosition
from config import settings


@dataclass
class MalfRunResult:
    """单标的 MALF 全链路输出（Core + Lifespan + v1.5 行为）。

    三组逐 bar 一一对应（同序，同 bar_dt）：snapshots / positions / behaviors。
    """

    symbol: str
    timeframe: str
    bars: list[DailyBar]
    core: CoreRunResult
    positions: list[WavePosition]
    behaviors: list[WaveBehaviorSnapshot]


def run_symbol(
    symbol: str,
    *,
    timeframe: str = "day",
    k: int = 2,
    start_dt: date | None = None,
    end_dt: date | None = None,
    price_line: str = settings.PRICE_LINE_STRUCTURE,
) -> tuple[list[DailyBar], CoreRunResult]:
    """加载单标的后复权日线并跑 Core 状态机。返回 (bars, result)。M1 兼容入口。"""
    bars = loader.load_bars(
        symbol, price_line=price_line, start_dt=start_dt, end_dt=end_dt
    )
    eng = CoreEngine(symbol, timeframe=timeframe, k=k, source_run_id="ui-inspect")
    result = eng.run(bars)
    return bars, result


def assemble_from_bars(
    bars: list[DailyBar],
    *,
    symbol: str,
    timeframe: str = "day",
    k: int = 2,
    source_run_id: str = "adhoc",
    lifespan_cfg: LifespanConfig | None = None,
    behavior_cfg: BehaviorConfig | None = None,
) -> MalfRunResult:
    """纯内存全链路组装：Core → Lifespan → v1.5 行为。不碰库，可单测。

    三组输出逐 bar 一一对应（O7 逐 bar 发布）。
    """
    eng = CoreEngine(symbol, timeframe=timeframe, k=k, source_run_id=source_run_id)
    core = eng.run(bars)
    positions = compute_wave_positions(
        core,
        bars,
        symbol=symbol,
        timeframe=timeframe,
        source_run_id=source_run_id,
        cfg=lifespan_cfg if lifespan_cfg is not None else LifespanConfig(),
    )
    behaviors = derive_behavior_snapshots(
        positions,
        core.snapshots,
        cfg=behavior_cfg if behavior_cfg is not None else BehaviorConfig(),
        source_run_id=source_run_id,
    )
    return MalfRunResult(
        symbol=symbol,
        timeframe=timeframe,
        bars=bars,
        core=core,
        positions=positions,
        behaviors=behaviors,
    )


def run_symbol_full(
    symbol: str,
    *,
    timeframe: str = "day",
    k: int = 2,
    start_dt: date | None = None,
    end_dt: date | None = None,
    price_line: str = settings.PRICE_LINE_STRUCTURE,
    source_run_id: str = "adhoc",
) -> MalfRunResult:
    """加载行情后跑完整 MALF 链路（Core + Lifespan + v1.5 行为）。"""
    bars = loader.load_bars(
        symbol, price_line=price_line, start_dt=start_dt, end_dt=end_dt
    )
    return assemble_from_bars(
        bars,
        symbol=symbol,
        timeframe=timeframe,
        k=k,
        source_run_id=source_run_id,
    )
