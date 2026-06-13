"""回测运行薄封装（镜像 malf/runner.py）。

把 loader/DB 挡在纯引擎之外：
- load 双价线（qfq_back 结构/盈亏价 + raw_none 涨跌停限价）+ PAS 快照预算（方案 A）。
- 从 config/params_default.toml 建 EngineConfig（tuning M5 可覆盖）。

无未来函数（方案 A）：每标的循环前一次性跑完 MALF→PAS（assemble_from_bars +
derive_pas_snapshots，已被既有测试覆盖、因果天然成立），引擎只读 snap_by_dt[bar_dt]。
"""

from __future__ import annotations

import tomllib
from datetime import date, datetime
from pathlib import Path

from asteria.backtest.broker import BrokerConfig
from asteria.backtest.engine import (
    BacktestEngine,
    BacktestRunResult,
    EngineConfig,
    SymbolData,
)
from asteria.backtest.rules import RulesConfig
from asteria.backtest.types import StructuralLevels
from asteria.data import loader
from asteria.data.universe import infer_board, is_probably_st
from asteria.malf.runner import assemble_from_bars
from asteria.pas.core import derive_pas_snapshots
from asteria.pas.types import DirectionalPremise, Posture, ReadStatus, SetupFamily
from asteria.signal.types import SignalConfig
from config import settings

_PARAMS_PATH = settings.REPO_ROOT / "config" / "params_default.toml"

_POSTURE_BY_NAME = {p.value: p for p in Posture}
_FAMILY_BY_NAME = {f.value: f for f in SetupFamily}
_READ_BY_NAME = {r.value: r for r in ReadStatus}
_PREMISE_BY_NAME = {p.value: p for p in DirectionalPremise}


def load_params(path: Path = _PARAMS_PATH) -> dict:
    """读 params_default.toml（tuning M5 可传别的 path / 覆盖后的 dict）。"""
    with path.open("rb") as f:
        return tomllib.load(f)


def build_engine_config(params: dict | None = None) -> EngineConfig:
    """从 params dict 装配 EngineConfig（缺省段落回退 dataclass 默认）。"""
    params = params if params is not None else load_params()
    sig = params.get("signal", {})
    bt = params.get("backtest", {})

    accepted_postures = frozenset(
        _POSTURE_BY_NAME[name]
        for name in sig.get("accept_postures", ["favored", "allowed"])
        if name in _POSTURE_BY_NAME
    )
    accept_families = frozenset(
        _FAMILY_BY_NAME[name]
        for name in sig.get("accept_families", [f.value for f in SetupFamily])
        if name in _FAMILY_BY_NAME
    )
    accepted_read = frozenset(
        _READ_BY_NAME[name]
        for name in sig.get("accept_read_status", ["strong", "mixed"])
        if name in _READ_BY_NAME
    )
    default_premises = [
        DirectionalPremise.EXPECT_STRENGTH_CONTINUATION.value,
        DirectionalPremise.EXPECT_BOUNDARY_TEST.value,
        DirectionalPremise.EXPECT_TRANSITION_RESOLUTION.value,
    ]
    actionable_premises = frozenset(
        _PREMISE_BY_NAME[name]
        for name in sig.get("actionable_premises", default_premises)
        if name in _PREMISE_BY_NAME
    )
    # life_state 上限（衰竭/末端排除）：toml 缺省用 dataclass 默认；显式空列表 → None（不卡）
    sig_defaults = SignalConfig()
    if "accept_life_states" in sig:
        names = sig["accept_life_states"]
        accepted_life = frozenset(names) if names else None
    else:
        accepted_life = sig_defaults.accepted_life_states
    signal_cfg = SignalConfig(
        accepted_postures=accepted_postures or frozenset({Posture.FAVORED, Posture.ALLOWED}),
        accept_families=accept_families or frozenset(SetupFamily),
        accepted_read_status=accepted_read or sig_defaults.accepted_read_status,
        actionable_premises=actionable_premises or sig_defaults.actionable_premises,
        accepted_life_states=accepted_life,
        min_quality_score=sig.get("min_quality_score", 2),
        veto_ambiguity_dominates=sig.get("veto_ambiguity_dominates", False),
        min_reward_risk=sig.get("min_reward_risk", 1.5),
        min_risk_pct=bt.get("min_risk_pct", 0.0),
        stop_offset=bt.get("stop_offset", 0.02),
        target_r=bt.get("target_r", 1.0),
        signal_rule_version=sig.get("signal_rule_version", sig_defaults.signal_rule_version),
    )
    rules_cfg = RulesConfig(
        stop_offset=bt.get("stop_offset", 0.02),
        target_r=bt.get("target_r", 1.0),
        scale_out_pct=bt.get("scale_out_pct", 0.5),
        time_stop_bars=bt.get("time_stop_bars", 8),
        trail_method=bt.get("trail_method", "prev_hl"),
        trail_k=bt.get("trail_k", 3.0),
        atr_period=bt.get("atr_period", 14),
        min_risk_pct=bt.get("min_risk_pct", 0.0),
    )
    broker_cfg = BrokerConfig(
        default_price_limit_pct=bt.get("default_price_limit_pct", 0.10),
    )
    return EngineConfig(
        signal=signal_cfg,
        broker=broker_cfg,
        rules=rules_cfg,
        initial_capital=bt.get("initial_capital", 1_000_000.0),
        position_pct_per_trade=bt.get("position_pct_per_trade", 0.1),
        market_filter_enabled=bt.get("market_filter_enabled", False),
        market_index_symbol=bt.get("market_index_symbol", "000300.SH"),
        bear_states=tuple(bt.get("bear_states", ["down_alive"])),
    )


def _make_run_id(group_name: str) -> str:
    return f"bt-{group_name}-{datetime.now().strftime('%Y%m%d%H%M%S')}"


def prepare_symbol(
    symbol: str,
    *,
    timeframe: str = "day",
    k: int = 2,
    start_dt: date | None = None,
    end_dt: date | None = None,
    source_run_id: str = "adhoc",
) -> SymbolData | None:
    """加载单标的双价线 + 预算 PAS 快照（方案 A）。无 qfq 数据返回 None。

    注意：MALF/PAS 用全量历史派生（因果天然成立），裁剪在引擎主日历层做。
    回测窗口外的早期 bar 仍参与结构识别，保证窗口起点的 PAS 快照已"预热"。
    """
    qfq_bars = loader.load_bars(
        symbol, price_line=settings.PRICE_LINE_STRUCTURE, end_dt=end_dt
    )
    if not qfq_bars:
        return None
    raw_bars = loader.load_bars(
        symbol, price_line=settings.PRICE_LINE_RAW, end_dt=end_dt
    )
    malf = assemble_from_bars(
        qfq_bars, symbol=symbol, timeframe=timeframe, k=k, source_run_id=source_run_id
    )
    snaps = derive_pas_snapshots(
        malf.positions, malf.behaviors, source_run_id=source_run_id
    )
    # 结构价投影：从 MALF Core 快照取前高/guard（与 snaps/qfq_bars 1:1，O7 逐 bar 发布）。
    # life_state 在 WavePosition（不在 CoreStateSnapshot），按 bar_dt 映射取入（质量门上限用）。
    life_by_dt = {
        p.bar_dt: (p.life_state.value if p.life_state else None) for p in malf.positions
    }
    cores = [
        StructuralLevels(
            bar_dt=s.bar_dt,
            progress_extreme_price=s.progress_extreme_price,
            guard_price=s.current_effective_guard_price,
            direction=s.direction,
            system_state=s.system_state.value if s.system_state else None,
            life_state=life_by_dt.get(s.bar_dt),
        )
        for s in malf.core.snapshots
    ]
    inst = loader.get_instrument(symbol)
    name = inst["name"] if inst is not None else None
    return SymbolData(
        symbol=symbol,
        qfq_bars=qfq_bars,
        raw_bars=raw_bars,
        snaps=snaps,
        cores=cores,
        board=infer_board(symbol),
        is_st=is_probably_st(name),
    )


def prepare_market_regime(
    index_symbol: str = "000300.SH",
    *,
    timeframe: str = "day",
    k: int = 2,
    end_dt: date | None = None,
    source_run_id: str = "adhoc",
) -> dict[date, str]:
    """用指数（沪深300）跑 MALF，取逐 bar system_state 作大盘趋势态（决策 B）。

    返回 {bar_dt → system_state.value}（up_alive/down_alive/transition/uninitialized）。
    无未来函数：assemble_from_bars 因果天然成立，引擎只读 regime[bar_dt]（方案 A）。
    指数无数据 → 返回空 dict（引擎按"无 regime 不过滤"处理）。
    """
    bars = loader.load_bars(
        index_symbol, price_line=settings.PRICE_LINE_STRUCTURE, end_dt=end_dt
    )
    if not bars:
        return {}
    malf = assemble_from_bars(
        bars, symbol=index_symbol, timeframe=timeframe, k=k, source_run_id=source_run_id
    )
    return {
        s.bar_dt: s.system_state.value
        for s in malf.core.snapshots
        if s.system_state is not None
    }


def run_backtest(
    symbols: list[str],
    *,
    start_dt: date | None = None,
    end_dt: date | None = None,
    group_name: str = "adhoc",
    timeframe: str = "day",
    k: int = 2,
    params: dict | None = None,
    source_run_id: str | None = None,
) -> BacktestRunResult:
    """端到端单组回测：load 双价线 → 预算 PAS → （可选）大盘 regime → 跑引擎。"""
    run_id = source_run_id or _make_run_id(group_name)
    cfg = build_engine_config(params)

    symbols_data: list[SymbolData] = []
    for sym in symbols:
        sd = prepare_symbol(
            sym,
            timeframe=timeframe,
            k=k,
            start_dt=start_dt,
            end_dt=end_dt,
            source_run_id=run_id,
        )
        if sd is not None:
            symbols_data.append(sd)

    # 大盘趋势过滤（决策 B/C）：启用时用指数 MALF system_state 作 regime
    market_regime: dict[date, str] | None = None
    if cfg.market_filter_enabled:
        market_regime = prepare_market_regime(
            cfg.market_index_symbol,
            timeframe=timeframe,
            k=k,
            end_dt=end_dt,
            source_run_id=run_id,
        )

    engine = BacktestEngine(cfg=cfg, source_run_id=run_id, group_name=group_name)
    return engine.run(
        symbols_data,
        start_dt=start_dt,
        end_dt=end_dt,
        market_regime_by_dt=market_regime,
    )
