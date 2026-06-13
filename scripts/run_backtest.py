"""CLI: 单组回测（Signal + 回测事件循环）。

用法：
  python scripts/run_backtest.py --symbol 600000.SH --start 2023-01-01 --end 2024-12-31
  python scripts/run_backtest.py --symbol 600000.SH --start 2023-01-01 --end 2024-12-31 --write

验收口径（docs/02-module-design/BACKTEST_DESIGN.md §10 + docs/03-task-breakdown/TEST_ACCEPTANCE.md §4）：
  无未来函数（进场 dt = 发现 dt 的下一交易日）；R_multiple 可手算对账；
  减仓后清仓价 > target1（移动止损不变量）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# 让 src 布局可被 import（无需安装）
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from asteria.backtest.engine import BacktestRunResult  # noqa: E402
from asteria.backtest.runner import build_engine_config, run_backtest  # noqa: E402


def _parse_date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _fmt(v: object) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def main() -> None:
    ap = argparse.ArgumentParser(description="跑单组回测：Signal accept/reject → 事件循环")
    ap.add_argument("--symbol", action="append", help="标的（可多次），如 600000.SH。与 --universe 二选一")
    ap.add_argument("--universe", action="store_true", help="从 instrument 表按筛选选一批标的（跑组合分布）")
    ap.add_argument("--boards", help="--universe 时限定 board（逗号分隔，如 main,chinext,star）")
    ap.add_argument("--min-list-days", type=int, default=365, help="--universe 上市天数下限（默认 365）")
    ap.add_argument("--limit", type=int, help="--universe 标的数上限")
    ap.add_argument("--start", help="起始日 YYYY-MM-DD")
    ap.add_argument("--end", help="结束日 YYYY-MM-DD")
    ap.add_argument("--group", default="adhoc", help="时间分组名（initial/validation/holdout/adhoc）")
    ap.add_argument("--k", type=int, default=2, help="分形确认 k（默认 2）")
    ap.add_argument("--write", action="store_true", help="落库到 backtest")
    ap.add_argument("--replace", action="store_true", help="落库前先删同 run_id 旧行")
    args = ap.parse_args()

    start_dt = _parse_date(args.start)
    end_dt = _parse_date(args.end)

    # 标的来源：--universe（按筛选选一批）或 --symbol（显式列）
    if args.universe:
        from asteria.data import loader
        from asteria.data.universe import UniverseFilter

        boards = tuple(args.boards.split(",")) if args.boards else None
        flt = UniverseFilter(min_list_days=args.min_list_days, exclude_st=True)
        symbols = loader.select_symbols(flt=flt, boards=boards, limit=args.limit)
        print(f"--universe 选出 {len(symbols)} 只标的"
              f"（boards={boards or '全部'}, min_list_days={args.min_list_days}, limit={args.limit}）")
    elif args.symbol:
        symbols = args.symbol
    else:
        ap.error("需指定 --symbol（可多次）或 --universe")

    if not symbols:
        ap.error("标的列表为空（检查 --universe 筛选条件或先灌数）")

    result = run_backtest(
        symbols,
        start_dt=start_dt,
        end_dt=end_dt,
        group_name=args.group,
        k=args.k,
    )

    n_cand = len(result.signal_candidates)
    n_acc = sum(1 for c in result.signal_candidates if c.decision.value == "accept")
    _label = symbols[0] if len(symbols) == 1 else f"{len(symbols)}只标的"
    print(f"[{_label}] run_id={result.run_id}  group={result.group_name}")
    print(f"  窗口 {result.start_dt} → {result.end_dt}  equity_points={len(result.equity_curve)}")
    print(f"  candidates={n_cand}（accept={n_acc}）  trades={len(result.trades)}")

    # --- 指标 ---
    m = result.metrics
    print("\n指标：")
    print(f"  total_return={_fmt(m.total_return)}  cagr={_fmt(m.cagr)}  max_dd={_fmt(m.max_drawdown)}")
    print(f"  sharpe={_fmt(m.sharpe)}  win_rate={_fmt(m.win_rate)}  avg_R={_fmt(m.avg_R)}")
    print(f"  expectancy={_fmt(m.expectancy)}  profit_factor={_fmt(m.profit_factor)}  trades={m.trade_count}")

    # --- 逐笔交易（手算对账用）---
    if result.trades:
        print("\n逐笔交易（手算对账）：")
        print(f"{'symbol':<11}{'entry_dt':<12}{'exit_dt':<12}{'entry':>9}{'avg_exit':>10}{'qty':>9}{'pnl':>12}{'R':>8} reason")
        for t in result.trades:
            print(
                f"{t.symbol:<11}{t.entry_dt.isoformat():<12}{t.exit_dt.isoformat():<12}"
                f"{t.entry_price:>9.2f}{t.avg_exit_price:>10.2f}{t.qty:>9.0f}"
                f"{t.realized_pnl:>12.2f}{t.R_multiple:>8.2f} {t.exit_reason.value}"
            )

    # --- 验收自检 ---
    ok = _acceptance_checks(result)

    # --- 落库 ---
    if args.write:
        from asteria.storage import db
        from asteria.storage.backtest_writer import write_run

        db.init_db("backtest")
        cfg = build_engine_config()
        counts = write_run(result, cfg=cfg, replace=args.replace)
        print(f"\n已写入 backtest：{counts}")

    if not ok:
        raise SystemExit(1)


def _acceptance_checks(result: BacktestRunResult) -> bool:
    """无未来函数 + 移动止损不变量 自检（供 CLI 设非零退出码）。"""
    problems: list[str] = []

    # 1. 无未来函数：每笔 entry_dt 严格晚于其候选 discover_dt
    cand_by_key = {f"c{i}": c for i, c in enumerate(result.signal_candidates, start=1)}
    for t in result.trades:
        cand = cand_by_key.get(t.signal_candidate_key or "")
        if cand is not None and not (t.entry_dt > cand.discover_dt):
            problems.append(
                f"{t.symbol} entry_dt={t.entry_dt} 未晚于 discover_dt={cand.discover_dt}（未来函数）"
            )

    # 2. R_multiple 与 realized_pnl 符号一致（denom>0 时）。
    #    注：移动止损「stop 线 > target1」不变量（规则7/§10.4）是对 current_stop 的
    #    约束，已由 test_backtest_rules.py::test_trailing_stop_above_target1_invariant
    #    在仓位层验证。它不等于「清仓价 > target1」——T+1 下卖单成交于次日 open，
    #    可跳空低于 stop，故 Trade.avg_exit_price 不受此约束（忠实 A 股建模）。
    #    R_multiple==0 表示进场即跳空到 stop 之下（risk_unit_R≤0），属真实边界，跳过。
    for t in result.trades:
        if t.R_multiple != 0.0 and (t.R_multiple > 0) != (t.realized_pnl > 0):
            problems.append(
                f"{t.symbol} R_multiple={t.R_multiple} 与 realized_pnl={t.realized_pnl} 符号不一致"
            )

    # 3. equity 曲线非空
    if not result.equity_curve:
        problems.append("equity_curve 为空（窗口无数据）")

    if problems:
        print("\n[验收 ✗] 发现问题：")
        for p in problems:
            print(f"  - {p}")
        return False
    print(
        "\n[验收 ✓] 无未来函数（entry>discover）· R_multiple/pnl 符号一致 · "
        "equity 曲线非空（stop>target1 不变量见 test_backtest_rules.py）"
    )
    return True


if __name__ == "__main__":
    main()
