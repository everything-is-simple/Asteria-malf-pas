"""回测事件循环引擎测试（精炼版：质量门 + 结构 T1/T2 + 保本跟踪）。

覆盖：
- 单标的手算逐字段对账（进场=次日 open / stop / 1R / 结构 T1/T2 / R_multiple）
- 无未来函数：entry_dt 严格晚于 discover_dt，等于下一交易日
- 循环步序：bar i 生成的买单在 bar i+1 撮合
- equity_curve 长度 = 主日历长度
- T2 出场（结构量度移动）

引擎需要 PASCoreSnapshot 驱动 Signal + StructuralLevels 提供结构价（RR≥1.5 门）。
这里直接构造 favored+strong 快照与结构价，绕过 MALF/PAS 真实派生（已被既有测试覆盖），
聚焦回测事件循环本身。
"""

from datetime import date, timedelta

from asteria.backtest.engine import (
    BacktestEngine,
    EngineConfig,
    SymbolData,
)
from asteria.backtest.rules import RulesConfig
from asteria.backtest.types import ExitReason, StructuralLevels
from asteria.data.contracts import DailyBar
from asteria.malf.types import Direction, SystemState
from asteria.pas.types import (
    DirectionalPremise,
    PASCoreSnapshot,
    Posture,
    ReadStatus,
)
from asteria.signal.types import SignalConfig

SYMBOL = "600000.SH"
D0 = date(2024, 1, 1)


def _d(i: int) -> date:
    """第 i 个连续日历日（测试用，无视周末）。"""
    return D0 + timedelta(days=i)


def _bar(i: int, o: float, h: float, low: float, c: float) -> DailyBar:
    return DailyBar(
        symbol=SYMBOL, bar_dt=_d(i), open=o, high=h, low=low, close=c,
        volume=1e6, amount=1e7,
    )


def _favored_snap(i: int, family: str = "PB") -> PASCoreSnapshot:
    """构造一个该 family = favored + strong 的快照（驱动 Signal accept，过质量门）。"""
    kwargs = dict(
        symbol=SYMBOL,
        timeframe="day",
        bar_dt=_d(i),
        wave_id=1,
        direction=Direction.UP,
        system_state=SystemState.UP_ALIVE.value,
        directional_premise=DirectionalPremise.EXPECT_STRENGTH_CONTINUATION,
        read_status=ReadStatus.STRONG,
        tst_posture=Posture.BLOCKED,
        bof_posture=Posture.BLOCKED,
        bpb_posture=Posture.BLOCKED,
        pb_posture=Posture.BLOCKED,
        cpb_posture=Posture.BLOCKED,
        strength_evidence_count=3,
        weakness_evidence_count=0,
        ambiguity_evidence_count=0,
    )
    kwargs[f"{family.lower()}_posture"] = Posture.FAVORED
    return PASCoreSnapshot(**kwargs)


def _blocked_snap(i: int) -> PASCoreSnapshot:
    """全 blocked，不产生候选。"""
    return PASCoreSnapshot(
        symbol=SYMBOL,
        timeframe="day",
        bar_dt=_d(i),
        wave_id=1,
        direction=Direction.UP,
        system_state=SystemState.UP_ALIVE.value,
        directional_premise=DirectionalPremise.NO_ACTIONABLE_PREMISE,
        read_status=ReadStatus.NOT_APPLICABLE,
        tst_posture=Posture.BLOCKED,
        bof_posture=Posture.BLOCKED,
        bpb_posture=Posture.BLOCKED,
        pb_posture=Posture.BLOCKED,
        cpb_posture=Posture.BLOCKED,
    )


def _levels(i: int, *, progress_extreme: float | None, guard: float | None) -> StructuralLevels:
    """构造单 bar 结构价投影。"""
    return StructuralLevels(
        bar_dt=_d(i),
        progress_extreme_price=progress_extreme,
        guard_price=guard,
        direction=Direction.UP,
        system_state=SystemState.UP_ALIVE.value,
    )


def _symbol_data(bars, snaps, cores=None) -> SymbolData:
    """raw 与 qfq 同价（测试不触发涨跌停）；cores 缺省全空（结构价 None → 1R 退化）。"""
    return SymbolData(
        symbol=SYMBOL,
        qfq_bars=bars,
        raw_bars=bars,
        snaps=snaps,
        cores=cores if cores is not None else [],
        board="main",
        is_st=False,
    )


# =========================================================================
# 单标的手算逐字段对账（结构 T1/T2 + 保本跟踪）
# =========================================================================
def test_single_symbol_trade_hand_reconciliation():
    """手算一笔完整交易：发现 → T+1 进场 → 达 T1 减半+保本 → 跟踪上移 → 清仓。

    序列（连续日历，主板，初始 100w，每笔 10%）：
      bar0  发现 T0：favored+strong PB；close=10.00 low=9.50；结构前高 11.06、guard 9.60
            stop=9.48、1R=0.52、rr_target=11.06 → RR=(11.06-10.00)/0.52=2.04≥1.5 ✓
      bar1  T1 进场：open=10.00 → entry=10.00；按实际 fill 重算：
            stop=9.48、1R=0.52、target1=min(11.06,10.52)=10.52、
            target2=11.06+(11.06-9.60)=12.52
      bar2  high=10.60 ≥ target1=10.52 → 减半意图 + 拉保本（current_stop→10.00）
      bar3  T+1 减半成交 @ open=10.60；half_exited；high=10.80<target2，
            跟踪：active_stop=max(10.00,10.00)=10.00；low=10.55>10.00 不破，
            current_stop=max(10.00, guard_price or low=10.55, 10.00)=10.55
      bar4  low=10.40 ≤ 10.55 → trailing 意图（T+1 卖）
      bar5  T+1 trailing 清仓 @ open=10.45（≥ 入场价 10.00，保本不变量成立；< target1 允许）
    """
    bars = [
        _bar(0, 10.00, 10.10, 9.50, 10.00),    # T0 发现
        _bar(1, 10.00, 10.20, 9.90, 10.10),    # T1 进场 @ open=10.00
        _bar(2, 10.10, 10.60, 10.05, 10.50),   # 达 target1 high=10.60≥10.52
        _bar(3, 10.60, 10.80, 10.55, 10.75),   # T+1 减半 @ open=10.60；止损→10.55
        _bar(4, 10.75, 10.78, 10.40, 10.50),   # low=10.40≤10.55 → trailing 触发
        _bar(5, 10.45, 10.60, 10.40, 10.55),   # T+1 trailing 清仓 @ open=10.45
        _bar(6, 10.50, 10.65, 10.45, 10.60),   # 收尾
    ]
    snaps = [_favored_snap(0, "PB")] + [_blocked_snap(i) for i in range(1, 7)]
    # 只有发现 bar 需要结构价；其余给 None（无候选，不影响）
    cores = [_levels(0, progress_extreme=11.06, guard=9.60)] + [
        _levels(i, progress_extreme=None, guard=None) for i in range(1, 7)
    ]

    cfg = EngineConfig(
        signal=SignalConfig(),
        rules=RulesConfig(stop_offset=0.02, target_r=1.0, scale_out_pct=0.5, time_stop_bars=99),
        initial_capital=1_000_000.0,
        position_pct_per_trade=0.1,
    )
    eng = BacktestEngine(cfg=cfg, source_run_id="test-recon", group_name="initial")
    result = eng.run([_symbol_data(bars, snaps, cores)])

    # --- 候选：bar0 一条 accept，RR 可变（对结构前高算）---
    accepts = [c for c in result.signal_candidates if c.decision.value == "accept"]
    assert len(accepts) == 1
    cand = accepts[0]
    assert cand.discover_dt == _d(0)
    assert cand.planned_stop == 9.48  # 9.50 - 0.02
    # RR 对结构前高 11.06 算（计划值用 T0 close=10.00 作预期开盘）
    assert abs(cand.reward_risk - (11.06 - 10.00) / 0.52) < 1e-4
    assert cand.reward_risk >= 1.5

    # --- 一笔交易 ---
    assert len(result.trades) == 1
    t = result.trades[0]

    # 无未来函数：进场 = 发现日的下一交易日
    assert t.entry_dt == _d(1)
    assert t.entry_dt > cand.discover_dt
    # 进场价 = T1 open（实际成交价，决策 3）
    assert t.entry_price == 10.00

    # 原始 qty：budget=100w*0.1=10w，entry=10.00 → 10000 股；减半 5000，剩 5000
    # 减半 @ bar3 open=10.60；剩余 trailing @ bar5 open=10.45
    # avg_exit = (10.60*5000 + 10.45*5000)/10000 = 10.525
    assert t.qty == 10000.0
    assert abs(t.avg_exit_price - 10.525) < 1e-6
    # realized = (10.60-10.00)*5000 + (10.45-10.00)*5000 = 3000 + 2250 = 5250
    assert abs(t.realized_pnl - 5250.0) < 1e-6
    # R_multiple = 5250 / (0.52 * 10000)
    assert abs(t.R_multiple - (5250.0 / (0.52 * 10000))) < 1e-4
    # 最终出场归因 = trailing；清仓价 10.45 ≥ 入场价 10.00（保本不变量），< target1 10.52（允许）
    assert t.exit_reason == ExitReason.TRAILING
    assert t.avg_exit_price >= t.entry_price


# =========================================================================
# T2 出场（结构量度移动）
# =========================================================================
def test_t2_exit_at_measured_move():
    """减半后价格冲到结构 T2 → TARGET2 清剩余，混合 Trade 归因 = target2。

    结构前高 11.06、guard 9.60 → target2=12.52。让价格 bar4 冲到 12.60≥12.52。
    """
    bars = [
        _bar(0, 10.00, 10.10, 9.50, 10.00),
        _bar(1, 10.00, 10.20, 9.90, 10.10),
        _bar(2, 10.10, 10.60, 10.05, 10.50),   # 达 target1 → 减半意图
        _bar(3, 10.60, 11.00, 10.55, 10.95),   # 减半成交；high<target2
        _bar(4, 11.00, 12.60, 10.95, 12.50),   # high=12.60≥target2=12.52 → TARGET2 意图
        _bar(5, 12.40, 12.60, 12.30, 12.50),   # T+1 清仓 @ open=12.40
        _bar(6, 12.40, 12.50, 12.30, 12.40),
    ]
    snaps = [_favored_snap(0, "PB")] + [_blocked_snap(i) for i in range(1, 7)]
    cores = [_levels(0, progress_extreme=11.06, guard=9.60)] + [
        _levels(i, progress_extreme=None, guard=None) for i in range(1, 7)
    ]
    cfg = EngineConfig(
        rules=RulesConfig(time_stop_bars=99),
    )
    eng = BacktestEngine(cfg=cfg, source_run_id="test-t2")
    result = eng.run([_symbol_data(bars, snaps, cores)])

    assert len(result.trades) == 1
    t = result.trades[0]
    # 最终一腿是 target2（混合 Trade exit_reason 取最后一腿）
    assert t.exit_reason == ExitReason.TARGET2
    # 出场价含减半(10.60)与 T2(12.40)两腿加权
    assert abs(t.avg_exit_price - (10.60 * 5000 + 12.40 * 5000) / 10000) < 1e-6


# =========================================================================
# 无未来函数
# =========================================================================
def test_no_future_function_entry_is_next_day():
    """每笔交易进场日严格晚于发现日，且 = 下一日历交易日。"""
    bars = [_bar(i, 10.0, 10.5, 9.8, 10.2) for i in range(6)]
    snaps = [_favored_snap(0, "PB")] + [_blocked_snap(i) for i in range(1, 6)]
    # 结构前高足够远使 RR≥1.5（10.0→11.0，stop=9.78，1R=0.42 → RR≈2.86）
    cores = [_levels(0, progress_extreme=11.0, guard=9.9)] + [
        _levels(i, progress_extreme=None, guard=None) for i in range(1, 6)
    ]
    eng = BacktestEngine(
        cfg=EngineConfig(rules=RulesConfig(time_stop_bars=99)),
        source_run_id="test-nofuture",
    )
    result = eng.run([_symbol_data(bars, snaps, cores)])
    accepts = [c for c in result.signal_candidates if c.decision.value == "accept"]
    assert accepts
    assert result.trades or eng.open_positions  # 至少进场了
    pos_or_trade_entry = (
        result.trades[0].entry_dt if result.trades
        else next(iter(eng.open_positions.values())).entry_dt
    )
    assert pos_or_trade_entry == _d(1)
    assert pos_or_trade_entry > accepts[0].discover_dt


def test_buy_order_fills_next_bar_not_same_bar():
    """循环步序：bar0 accept 生成的买单在 bar1 撮合，bar0 当日无持仓。"""
    bars = [_bar(i, 10.0, 10.5, 9.8, 10.2) for i in range(4)]
    snaps = [_favored_snap(0, "PB")] + [_blocked_snap(i) for i in range(1, 4)]
    cores = [_levels(0, progress_extreme=11.0, guard=9.9)] + [
        _levels(i, progress_extreme=None, guard=None) for i in range(1, 4)
    ]
    eng = BacktestEngine(
        cfg=EngineConfig(rules=RulesConfig(time_stop_bars=99)),
        source_run_id="test-step",
    )
    eng.run([_symbol_data(bars, snaps, cores)])
    # bar0 净值快照时不应已有持仓（买单尚未撮合）
    assert eng.equity_curve[0].open_positions == 0
    # bar1 起应有持仓
    assert eng.equity_curve[1].open_positions == 1


# =========================================================================
# equity_curve 长度 + 无候选
# =========================================================================
def test_equity_curve_matches_calendar_length():
    """逐 bar 记一次净值，曲线长度 = 主日历长度。"""
    bars = [_bar(i, 10.0, 10.5, 9.8, 10.2) for i in range(10)]
    snaps = [_blocked_snap(i) for i in range(10)]  # 无候选
    eng = BacktestEngine(source_run_id="test-eq")
    result = eng.run([_symbol_data(bars, snaps)])
    assert len(result.equity_curve) == 10
    # 无交易 → 净值恒等于初始资金
    assert all(abs(p.equity - 1_000_000.0) < 1e-6 for p in result.equity_curve)
    assert result.metrics.trade_count == 0


def test_no_candidate_when_all_blocked():
    """全 blocked 快照 → select_candidate 返回 None，不记录候选。"""
    bars = [_bar(i, 10.0, 10.5, 9.8, 10.2) for i in range(5)]
    snaps = [_blocked_snap(i) for i in range(5)]
    eng = BacktestEngine(source_run_id="test-noblock")
    result = eng.run([_symbol_data(bars, snaps)])
    assert result.signal_candidates == []
    assert result.trades == []


def test_no_structural_target_rejects_under_rr_gate():
    """有 favored+strong 但无结构前高 → RR=1.0 < 1.5 → 不开仓（reject）。"""
    bars = [_bar(i, 10.0, 10.5, 9.8, 10.2) for i in range(5)]
    snaps = [_favored_snap(0, "PB")] + [_blocked_snap(i) for i in range(1, 5)]
    # 无结构价（cores 全空）→ RR 退 1.0 < 1.5
    eng = BacktestEngine(source_run_id="test-norr")
    result = eng.run([_symbol_data(bars, snaps)])
    rejects = [c for c in result.signal_candidates if c.decision.value == "reject"]
    assert rejects  # 记录了 reject
    assert result.trades == []  # 没开仓


# =========================================================================
# 大盘趋势过滤（决策 B/C/D：down_alive 停开新多单，已持仓不强平）
# =========================================================================
def _favored_setup():
    """构造一个会开多单的 setup（favored+strong + 结构前高使 RR≥1.5）。"""
    bars = [_bar(i, 10.0, 10.5, 9.8, 10.2) for i in range(6)]
    snaps = [_favored_snap(0, "PB")] + [_blocked_snap(i) for i in range(1, 6)]
    cores = [_levels(0, progress_extreme=11.0, guard=9.9)] + [
        _levels(i, progress_extreme=None, guard=None) for i in range(1, 6)
    ]
    return bars, snaps, cores


def test_market_filter_blocks_entry_in_bear():
    """发现日大盘 down_alive → 停开新多单（无候选记录、无交易）。"""
    bars, snaps, cores = _favored_setup()
    cfg = EngineConfig(
        rules=RulesConfig(time_stop_bars=99),
        market_filter_enabled=True,
        bear_states=("down_alive",),
    )
    eng = BacktestEngine(cfg=cfg, source_run_id="test-bear")
    regime = {_d(i): "down_alive" for i in range(6)}  # 全程熊市
    result = eng.run([_symbol_data(bars, snaps, cores)], market_regime_by_dt=regime)
    assert result.trades == []
    assert eng.open_positions == {}
    # down_alive 当日直接 return，不进入候选收集
    assert result.signal_candidates == []


def test_market_filter_allows_entry_in_bull():
    """发现日大盘 up_alive → 正常开多单（过滤不误伤）。"""
    bars, snaps, cores = _favored_setup()
    cfg = EngineConfig(
        rules=RulesConfig(time_stop_bars=99),
        market_filter_enabled=True,
        bear_states=("down_alive",),
    )
    eng = BacktestEngine(cfg=cfg, source_run_id="test-bull")
    regime = {_d(i): "up_alive" for i in range(6)}
    result = eng.run([_symbol_data(bars, snaps, cores)], market_regime_by_dt=regime)
    accepts = [c for c in result.signal_candidates if c.decision.value == "accept"]
    assert accepts  # 牛市照常开
    assert result.trades or eng.open_positions


def test_market_filter_disabled_ignores_regime():
    """market_filter_enabled=False → 即使传 down_alive regime 也不过滤。"""
    bars, snaps, cores = _favored_setup()
    cfg = EngineConfig(rules=RulesConfig(time_stop_bars=99), market_filter_enabled=False)
    eng = BacktestEngine(cfg=cfg, source_run_id="test-off")
    regime = {_d(i): "down_alive" for i in range(6)}
    result = eng.run([_symbol_data(bars, snaps, cores)], market_regime_by_dt=regime)
    accepts = [c for c in result.signal_candidates if c.decision.value == "accept"]
    assert accepts  # 未启用过滤 → 照常开


def test_market_filter_does_not_force_close_existing():
    """已持仓后转熊（决策 D）→ 不强平，靠止损/跟踪自然出场。

    bar0 牛市开单，bar2 起转 down_alive。验证：转熊不触发"组合层强平"——
    持仓按交易规则（target1/trailing/stop）自然走完生命周期，出场归因是规则而非强平。
    """
    bars, snaps, cores = _favored_setup()
    cfg = EngineConfig(
        rules=RulesConfig(time_stop_bars=99),
        market_filter_enabled=True,
        bear_states=("down_alive",),
    )
    eng = BacktestEngine(cfg=cfg, source_run_id="test-hold")
    regime = {_d(0): "up_alive", _d(1): "up_alive"}
    regime.update({_d(i): "down_alive" for i in range(2, 6)})
    result = eng.run([_symbol_data(bars, snaps, cores)], market_regime_by_dt=regime)
    # bar1 进场（牛市发现 bar0 → T+1 进场）
    assert eng.equity_curve[1].open_positions == 1
    # 转熊后持仓仍在推进（bar2/3 仍持有，未被大盘过滤强平）
    assert eng.equity_curve[2].open_positions == 1
    # 最终出场归因是交易规则（trailing/target/stop），不存在"大盘强平"这种归因
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason.value in {
        "trailing", "target1", "target2", "stop", "time_stop", "breakdown"
    }

