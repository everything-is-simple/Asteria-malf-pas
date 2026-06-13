"""回测结果持久化（镜像 malf_writer.py:write_run）。

把 BacktestRunResult 写入 backtest 库 6 表，FK 安全顺序：
  1. param_set        参数网格的一个点（params_json）
  2. backtest_run     一次回测（PK=run_id，绑定 param_set + group_name）
  3. signal_candidate 每个机会（accept + reject 都记），捕获 lastrowid 建 key→db_id 映射
  4. bt_trade         逐笔成交（signal_candidate_id 由 engine key 解析）
  5. bt_equity_curve  逐 bar 净值（executemany）
  6. bt_metrics       汇总

run_id 在 runner 生成一次，同时作 source_run_id 与 backtest_run.run_id。
backtest 表无业务 UNIQUE 键——新 run_id 直接追加；可选 --replace 先删旧行。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime

from asteria.backtest.engine import BacktestRunResult, EngineConfig
from asteria.backtest.metrics import BtMetrics
from asteria.backtest.types import Trade
from asteria.signal.types import SignalCandidate
from asteria.storage import db


def _enum_val(v: object) -> object:
    """枚举取 .value，其余原样（None 保持 None）。"""
    return v.value if hasattr(v, "value") else v


def _config_to_json(cfg: EngineConfig) -> str:
    """EngineConfig → params_json（frozenset/枚举转可序列化）。"""

    def _default(o: object) -> object:
        if isinstance(o, frozenset):
            return sorted(_enum_val(x) for x in o)
        if hasattr(o, "value"):
            return o.value
        if is_dataclass(o) and not isinstance(o, type):
            return asdict(o)
        return str(o)

    payload = {
        "signal": asdict(cfg.signal),
        "broker": asdict(cfg.broker),
        "rules": asdict(cfg.rules),
        "initial_capital": cfg.initial_capital,
        "position_pct_per_trade": cfg.position_pct_per_trade,
    }
    return json.dumps(payload, ensure_ascii=False, default=_default)


_PARAM_SET_SQL = """
INSERT INTO param_set (name, params_json, created_at) VALUES (?,?,?)
"""

_RUN_SQL = """
INSERT OR REPLACE INTO backtest_run (
    run_id, param_set_id, group_name, start_dt, end_dt, universe_filter,
    created_at, status
) VALUES (?,?,?,?,?,?,?,?)
"""

_CAND_SQL = """
INSERT INTO signal_candidate (
    run_id, symbol, discover_dt, setup_family, directional_premise, read_status,
    planned_entry, planned_stop, planned_target1, planned_target2, reward_risk,
    decision, reason
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_TRADE_SQL = """
INSERT INTO bt_trade (
    run_id, symbol, entry_dt, exit_dt, entry_price, avg_exit_price, qty,
    realized_pnl, R_multiple, exit_reason, signal_candidate_id
) VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""

_EQUITY_SQL = """
INSERT OR REPLACE INTO bt_equity_curve (run_id, bar_dt, equity, cash, open_positions)
VALUES (?,?,?,?,?)
"""

_METRICS_SQL = """
INSERT OR REPLACE INTO bt_metrics (
    run_id, total_return, cagr, max_drawdown, sharpe, win_rate, avg_R,
    expectancy, trade_count, profit_factor
) VALUES (?,?,?,?,?,?,?,?,?,?)
"""


def _cand_row(run_id: str, c: SignalCandidate) -> tuple:
    return (
        run_id,
        c.symbol,
        c.discover_dt.isoformat(),
        _enum_val(c.setup_family),
        _enum_val(c.directional_premise),
        _enum_val(c.read_status),
        c.planned_entry,
        c.planned_stop,
        c.planned_target1,
        c.planned_target2,
        c.reward_risk,
        _enum_val(c.decision),
        c.reason,
    )


def _trade_row(run_id: str, t: Trade, candidate_id: int | None) -> tuple:
    return (
        run_id,
        t.symbol,
        t.entry_dt.isoformat() if t.entry_dt else None,
        t.exit_dt.isoformat() if t.exit_dt else None,
        t.entry_price,
        t.avg_exit_price,
        t.qty,
        t.realized_pnl,
        t.R_multiple,
        _enum_val(t.exit_reason),
        candidate_id,
    )


def _metrics_row(run_id: str, m: BtMetrics) -> tuple:
    return (
        run_id,
        m.total_return,
        m.cagr,
        m.max_drawdown,
        m.sharpe,
        m.win_rate,
        m.avg_R,
        m.expectancy,
        m.trade_count,
        m.profit_factor,
    )


def write_run(
    result: BacktestRunResult,
    *,
    cfg: EngineConfig,
    universe_filter: str = "",
    replace: bool = False,
    con: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """把回测结果写入 backtest 库。返回各表写入行数。

    调用方需保证库已建（db.init_db("backtest")）。
    replace=True 时先删该 run_id 旧行（幂等重跑）。
    """
    own = con is None
    con = con or db.connect("backtest")
    try:
        run_id = result.run_id
        now = datetime.now().isoformat(timespec="seconds")

        if replace:
            for tbl in ("signal_candidate", "bt_trade", "bt_equity_curve", "bt_metrics"):
                con.execute(f"DELETE FROM {tbl} WHERE run_id=?", (run_id,))

        # 1. param_set
        cur = con.execute(_PARAM_SET_SQL, (run_id, _config_to_json(cfg), now))
        param_set_id = cur.lastrowid

        # 2. backtest_run
        con.execute(
            _RUN_SQL,
            (
                run_id,
                param_set_id,
                result.group_name,
                result.start_dt.isoformat() if result.start_dt else None,
                result.end_dt.isoformat() if result.end_dt else None,
                universe_filter,
                now,
                "done",
            ),
        )

        # 3. signal_candidate（逐行 insert 捕获 db id，建 engine_key → db_id 映射）
        key_to_id: dict[str, int] = {}
        # 候选与 engine 内部 key 的关联：candidate 上没存 key，用其在 accept 流程里
        # 生成的 key 不可见——改用 (symbol, discover_dt, setup_family, decision) 匹配 trade。
        # 但 trade 持有 signal_candidate_key（c{N}）。为可靠关联，按候选插入顺序
        # 重建 key：engine 生成 key 的顺序 == signal_candidates 追加顺序，
        # accept 的候选 key = c{该候选在所有候选中的序号}。
        # 这里直接用 candidate 在列表中的 1-based 序号作 key（与 engine _cand_seq 一致）。
        for idx, cand in enumerate(result.signal_candidates, start=1):
            cur = con.execute(_CAND_SQL, _cand_row(run_id, cand))
            key_to_id[f"c{idx}"] = cur.lastrowid

        # 4. bt_trade（解析 signal_candidate_key → db id）
        trade_rows = [
            _trade_row(run_id, t, key_to_id.get(t.signal_candidate_key or ""))
            for t in result.trades
        ]
        con.executemany(_TRADE_SQL, trade_rows)

        # 5. bt_equity_curve
        equity_rows = [
            (run_id, p.bar_dt.isoformat(), p.equity, p.cash, p.open_positions)
            for p in result.equity_curve
        ]
        con.executemany(_EQUITY_SQL, equity_rows)

        # 6. bt_metrics
        con.execute(_METRICS_SQL, _metrics_row(run_id, result.metrics))

        con.commit()
        return {
            "signal_candidates": len(result.signal_candidates),
            "trades": len(trade_rows),
            "equity_points": len(equity_rows),
            "metrics": 1,
        }
    finally:
        if own:
            con.close()
