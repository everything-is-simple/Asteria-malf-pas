"""CLI: 回测 run 的分布分析（只读 backtest 库）。

第1套方法验证用——补足 compute_metrics 的 9 个汇总指标之外的「方法质量」视图：
  R 分布直方图/分位数 · exit_reason 占比+毛盈亏贡献 · target2 命中率
  · 按 setup_family/read_status 分层 · reject 原因占比。

🔒 严格分层：只读 backtest 库（connect_ro），不碰 data→malf→pas→signal→backtest 核心。
🔒 holdout 纪律：load_run 遇 group_name='holdout' 直接 SystemExit 拦截。

用法：
  python scripts/analyze_run.py --run-id bt-initial-20260613...
  python scripts/analyze_run.py --latest-group initial,validation   # 各取最新 run 并排对比
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# 让 src 布局可被 import（无需安装）
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from asteria.storage import db  # noqa: E402

# RejectReason 枚举值（与 signal/types.py 对齐，reject 占比文本匹配用）
_REJECT_REASONS = [
    "family_not_accepted",
    "posture_not_allowed",
    "read_status_too_weak",
    "life_state_exhausted",
    "quality_below_min",
    "ambiguity_dominates",
    "rr_below_min",
    "not_tradable",
]

# R 直方图分桶边界（左闭右开，末桶含上界）
_R_BUCKETS = [
    ("<-1", float("-inf"), -1.0),
    ("-1~0", -1.0, 0.0),
    ("0~1", 0.0, 1.0),
    ("1~2", 1.0, 2.0),
    ("2~5", 2.0, 5.0),
    (">5", 5.0, float("inf")),
]


# =========================================================================
# 加载（只读，holdout 拦截）
# =========================================================================
def load_run(con: sqlite3.Connection, run_id: str) -> dict:
    """读单个 run 的 backtest_run/bt_metrics/bt_trade/signal_candidate。

    🔒 group_name='holdout' → SystemExit（本验证阶段绝不分析 holdout）。
    """
    run_row = con.execute(
        "SELECT run_id, group_name, start_dt, end_dt, universe_filter FROM backtest_run WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if run_row is None:
        raise SystemExit(f"[analyze] run_id 不存在：{run_id}")
    group_name = run_row["group_name"]
    if group_name == "holdout":
        raise SystemExit(
            f"🔒 [analyze] 拒绝分析 holdout run（{run_id}）——holdout 是最终验证集，"
            f"本阶段只在 initial/validation 验证。"
        )
    metrics = con.execute(
        "SELECT * FROM bt_metrics WHERE run_id=?", (run_id,)
    ).fetchone()
    trades = con.execute(
        "SELECT * FROM bt_trade WHERE run_id=? ORDER BY entry_dt", (run_id,)
    ).fetchall()
    cands = con.execute(
        "SELECT * FROM signal_candidate WHERE run_id=?", (run_id,)
    ).fetchall()
    return {
        "run_id": run_id,
        "group_name": group_name,
        "start_dt": run_row["start_dt"],
        "end_dt": run_row["end_dt"],
        "universe_filter": run_row["universe_filter"],
        "metrics": metrics,
        "trades": trades,
        "cands": cands,
    }


def latest_run_id(con: sqlite3.Connection, group_name: str) -> str | None:
    """取某组最新（created_at 最大）的 run_id。"""
    row = con.execute(
        "SELECT run_id FROM backtest_run WHERE group_name=? ORDER BY created_at DESC LIMIT 1",
        (group_name,),
    ).fetchone()
    return row["run_id"] if row else None


# =========================================================================
# 纯统计函数（输入 sqlite3.Row 列表，无 I/O）
# =========================================================================
def _percentile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数（q∈[0,1]）。sorted_vals 已升序、非空。"""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def r_distribution(trades: list) -> dict:
    """R_multiple 分位数 + 直方图分桶。"""
    rs = sorted(t["R_multiple"] for t in trades if t["R_multiple"] is not None)
    if not rs:
        return {"n": 0, "quantiles": {}, "buckets": {}}
    quantiles = {
        "min": rs[0],
        "p5": _percentile(rs, 0.05),
        "p25": _percentile(rs, 0.25),
        "p50": _percentile(rs, 0.50),
        "p75": _percentile(rs, 0.75),
        "p95": _percentile(rs, 0.95),
        "max": rs[-1],
        "mean": sum(rs) / len(rs),
    }
    buckets = {label: 0 for label, _, _ in _R_BUCKETS}
    for r in rs:
        for label, lo, hi in _R_BUCKETS:
            # 左闭右开；末桶 (>5) 的 hi=inf 自然含上界
            if lo <= r < hi:
                buckets[label] += 1
                break
    return {"n": len(rs), "quantiles": quantiles, "buckets": buckets}


def exit_reason_breakdown(trades: list) -> dict:
    """按 exit_reason 统计笔数占比 + 各自毛盈亏占比。"""
    n = len(trades)
    out: dict[str, dict] = {}
    if n == 0:
        return out
    gross_win = sum(t["realized_pnl"] for t in trades if t["realized_pnl"] and t["realized_pnl"] > 0)
    gross_loss = -sum(t["realized_pnl"] for t in trades if t["realized_pnl"] and t["realized_pnl"] < 0)
    by_reason: dict[str, list] = {}
    for t in trades:
        by_reason.setdefault(t["exit_reason"] or "?", []).append(t)
    for reason, ts in sorted(by_reason.items()):
        pnl = sum(t["realized_pnl"] or 0.0 for t in ts)
        win_contrib = sum(t["realized_pnl"] for t in ts if t["realized_pnl"] and t["realized_pnl"] > 0)
        loss_contrib = -sum(t["realized_pnl"] for t in ts if t["realized_pnl"] and t["realized_pnl"] < 0)
        out[reason] = {
            "count": len(ts),
            "pct": len(ts) / n,
            "net_pnl": pnl,
            "win_share": (win_contrib / gross_win) if gross_win > 0 else 0.0,
            "loss_share": (loss_contrib / gross_loss) if gross_loss > 0 else 0.0,
            "avg_R": sum(t["R_multiple"] or 0.0 for t in ts) / len(ts),
        }
    return out


def target2_hit_rate(trades: list) -> float:
    """exit_reason='target2' 笔数 / 总成交笔数。"""
    n = len(trades)
    if n == 0:
        return 0.0
    hits = sum(1 for t in trades if t["exit_reason"] == "target2")
    return hits / n


def layered_stats(trades: list, cands: list) -> dict:
    """按 setup_family / read_status 分层算 avg_R/win_rate/count。

    经 bt_trade.signal_candidate_id → signal_candidate.signal_candidate_id join。
    """
    cand_by_id = {c["signal_candidate_id"]: c for c in cands}
    by_family: dict[str, list] = {}
    by_read: dict[str, list] = {}
    for t in trades:
        c = cand_by_id.get(t["signal_candidate_id"])
        fam = (c["setup_family"] if c else None) or "?"
        read = (c["read_status"] if c else None) or "?"
        by_family.setdefault(fam, []).append(t)
        by_read.setdefault(read, []).append(t)

    def _agg(group: dict[str, list]) -> dict:
        res = {}
        for k, ts in sorted(group.items()):
            wins = sum(1 for t in ts if (t["realized_pnl"] or 0.0) > 0)
            res[k] = {
                "count": len(ts),
                "win_rate": wins / len(ts) if ts else 0.0,
                "avg_R": sum(t["R_multiple"] or 0.0 for t in ts) / len(ts) if ts else 0.0,
                "net_pnl": sum(t["realized_pnl"] or 0.0 for t in ts),
            }
        return res

    return {"by_family": _agg(by_family), "by_read_status": _agg(by_read)}


def reject_breakdown(cands: list) -> dict:
    """reject 候选按 reason（文本匹配 RejectReason 枚举值）计占比。"""
    rejects = [c for c in cands if c["decision"] == "reject"]
    n = len(rejects)
    counts = {r: 0 for r in _REJECT_REASONS}
    other = 0
    for c in rejects:
        reason = (c["reason"] or "").strip()
        matched = next((r for r in _REJECT_REASONS if r in reason), None)
        if matched:
            counts[matched] += 1
        else:
            other += 1
    accept_n = sum(1 for c in cands if c["decision"] == "accept")
    return {
        "total_candidates": len(cands),
        "accept": accept_n,
        "reject": n,
        "accept_rate": accept_n / len(cands) if cands else 0.0,
        "by_reason": {r: counts[r] for r in _REJECT_REASONS if counts[r] > 0},
        "other": other,
    }


# =========================================================================
# 输出
# =========================================================================
def _fmt(v, nd: int = 4) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _summary_line(m) -> str:
    if m is None:
        return "  (无 bt_metrics)"
    return (
        f"  total_return={_fmt(m['total_return'])}  cagr={_fmt(m['cagr'])}  "
        f"max_dd={_fmt(m['max_drawdown'])}  sharpe={_fmt(m['sharpe'])}\n"
        f"  win_rate={_fmt(m['win_rate'])}  avg_R={_fmt(m['avg_R'])}  "
        f"expectancy={_fmt(m['expectancy'], 2)}  profit_factor={_fmt(m['profit_factor'])}  "
        f"trades={m['trade_count']}"
    )


def print_report(run: dict) -> None:
    """单组完整分布报告。"""
    print("=" * 72)
    print(f"run_id={run['run_id']}  group={run['group_name']}  窗口 {run['start_dt']} → {run['end_dt']}")
    if run["universe_filter"]:
        print(f"universe: {run['universe_filter']}")
    print("=" * 72)
    print("\n[汇总指标]")
    print(_summary_line(run["metrics"]))

    trades, cands = run["trades"], run["cands"]

    # reject 占比
    rb = reject_breakdown(cands)
    print(f"\n[候选筛选] 总候选={rb['total_candidates']}  accept={rb['accept']}"
          f"（{rb['accept_rate']:.1%}）  reject={rb['reject']}")
    for reason, cnt in rb["by_reason"].items():
        pct = cnt / rb["reject"] if rb["reject"] else 0.0
        print(f"    reject:{reason:<22} {cnt:>5}（{pct:.1%}）")
    if rb["other"]:
        print(f"    reject:{'(未匹配)':<22} {rb['other']:>5}")

    # R 分布
    rd = r_distribution(trades)
    print(f"\n[R 分布] n={rd['n']}")
    if rd["n"]:
        q = rd["quantiles"]
        print(f"    min={_fmt(q['min'],2)} p5={_fmt(q['p5'],2)} p25={_fmt(q['p25'],2)} "
              f"p50={_fmt(q['p50'],2)} p75={_fmt(q['p75'],2)} p95={_fmt(q['p95'],2)} "
              f"max={_fmt(q['max'],2)} mean={_fmt(q['mean'],3)}")
        print("    直方图：" + "  ".join(
            f"{label}:{rd['buckets'][label]}" for label, _, _ in _R_BUCKETS
        ))

    # exit_reason 占比
    eb = exit_reason_breakdown(trades)
    print("\n[出场归因] reason        笔数   占比    净盈亏      毛盈贡献  毛亏贡献  avg_R")
    for reason, st in eb.items():
        print(f"    {reason:<12} {st['count']:>5}  {st['pct']:>5.1%}  "
              f"{st['net_pnl']:>11.0f}  {st['win_share']:>7.1%}  {st['loss_share']:>7.1%}  "
              f"{st['avg_R']:>6.2f}")
    print(f"    target2 命中率（target2笔/总笔）= {target2_hit_rate(trades):.1%}")

    # 分层
    ls = layered_stats(trades, cands)
    print("\n[按 setup_family 分层]  family  笔数  win_rate  avg_R   净盈亏")
    for fam, st in ls["by_family"].items():
        print(f"    {fam:<8} {st['count']:>5}  {st['win_rate']:>7.1%}  {st['avg_R']:>6.2f}  {st['net_pnl']:>11.0f}")
    print("\n[按 read_status 分层]  read    笔数  win_rate  avg_R   净盈亏")
    for read, st in ls["by_read_status"].items():
        print(f"    {read:<8} {st['count']:>5}  {st['win_rate']:>7.1%}  {st['avg_R']:>6.2f}  {st['net_pnl']:>11.0f}")
    print()


def print_comparison(run_a: dict, run_b: dict) -> None:
    """两组并排对比（稳健性判断核心）。"""
    print_report(run_a)
    print_report(run_b)
    print("=" * 72)
    print(f"[跨组对比] {run_a['group_name']}  vs  {run_b['group_name']}")
    print("=" * 72)
    ma, mb = run_a["metrics"], run_b["metrics"]
    if ma and mb:
        keys = [
            ("profit_factor", "profit_factor"),
            ("avg_R", "avg_R"),
            ("win_rate", "win_rate"),
            ("total_return", "total_return"),
            ("max_drawdown", "max_drawdown"),
            ("sharpe", "sharpe"),
            ("trade_count", "trade_count"),
        ]
        print(f"    {'指标':<16}{run_a['group_name']:>14}{run_b['group_name']:>14}")
        for label, k in keys:
            print(f"    {label:<16}{_fmt(ma[k]):>14}{_fmt(mb[k]):>14}")
    print("\n  判读：两组 profit_factor/avg_R 是否都 >0 且未大幅退化？"
          "→ 决定第1套是否稳健（据实定量化门槛）。\n")


# =========================================================================
# CLI
# =========================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="回测 run 分布分析（只读 backtest 库）")
    ap.add_argument("--run-id", action="append", help="run_id（可多次）")
    ap.add_argument("--latest-group", help="按组取最新 run 并排，如 initial,validation")
    args = ap.parse_args()

    con = db.connect_ro("backtest")
    try:
        run_ids: list[str] = []
        if args.latest_group:
            for g in args.latest_group.split(","):
                g = g.strip()
                rid = latest_run_id(con, g)
                if rid is None:
                    print(f"[analyze] 组 {g} 无 run（先跑 --write 落库）")
                else:
                    run_ids.append(rid)
        if args.run_id:
            run_ids.extend(args.run_id)
        if not run_ids:
            ap.error("需指定 --run-id（可多次）或 --latest-group")

        runs = [load_run(con, rid) for rid in run_ids]
        if len(runs) == 2:
            print_comparison(runs[0], runs[1])
        else:
            for run in runs:
                print_report(run)
    finally:
        con.close()


if __name__ == "__main__":
    main()
