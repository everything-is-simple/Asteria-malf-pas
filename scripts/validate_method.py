"""CLI: 第1套方法的跨时间组验证编排。

固定参数（params_default.toml）在 initial(2018-2020) 与 validation(2021-2023) 两组各跑一次、
落库、然后并排对比分布——判断第1套是否稳健（非 2023 年碰运气）。非 tuning（不扫参数网格）。

🔒 holdout 纪律：只接受 {initial, validation}；显式拦截 holdout（最终验证集，本阶段绝不跑）。

用法：
  python scripts/validate_method.py --boards main --limit 200 --min-list-days 365
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

from asteria.backtest.runner import build_engine_config, run_backtest  # noqa: E402
from asteria.data import loader  # noqa: E402
from asteria.data.universe import UniverseFilter  # noqa: E402
from asteria.storage import db  # noqa: E402
from asteria.storage.backtest_writer import write_run  # noqa: E402
from config import settings  # noqa: E402

# 本阶段允许验证的时间组（holdout 绝不在内）
_ALLOWED_GROUPS = ("initial", "validation")


def _group_window(group: str) -> tuple[date, date]:
    """由 settings.GROUP_YEARS 推窗口 [年初, 年末]。"""
    years = settings.GROUP_YEARS[group]
    return date(years[0], 1, 1), date(years[-1], 12, 31)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="第1套方法跨时间组验证（initial + validation，落库 + 并排对比）"
    )
    ap.add_argument("--boards", help="限定 board（逗号分隔，如 main,chinext,star）")
    ap.add_argument("--min-list-days", type=int, default=365, help="上市天数下限（默认 365）")
    ap.add_argument("--limit", type=int, help="标的数上限")
    ap.add_argument("--k", type=int, default=2, help="分形确认 k（默认 2）")
    ap.add_argument(
        "--groups", default="initial,validation",
        help="验证时间组（默认 initial,validation；🔒 holdout 被拦截）",
    )
    args = ap.parse_args()

    groups = [g.strip() for g in args.groups.split(",")]
    for g in groups:
        if g == "holdout":
            raise SystemExit(
                "🔒 [validate] 拒绝跑 holdout——holdout(2024-2026) 是最终验证集，"
                "本阶段只在 initial/validation 验证。"
            )
        if g not in _ALLOWED_GROUPS:
            raise SystemExit(f"[validate] 未知时间组：{g}（只允许 {_ALLOWED_GROUPS}）")

    # 选池（一次选定，两组复用同一标的列表，保证可比）
    boards = tuple(args.boards.split(",")) if args.boards else None
    flt = UniverseFilter(
        min_list_days=args.min_list_days,
        exclude_st=True,
    )
    symbols = loader.select_symbols(flt=flt, boards=boards, limit=args.limit)
    universe_filter = (
        f"boards={boards or 'all'},min_list_days={args.min_list_days},"
        f"limit={args.limit},n={len(symbols)}"
    )
    print(f"[validate] 选出 {len(symbols)} 只标的（{universe_filter}）")
    if not symbols:
        raise SystemExit("[validate] 标的列表为空（检查筛选条件或先灌数）")

    db.init_db("backtest")
    cfg = build_engine_config()

    run_ids: list[str] = []
    for group in groups:
        start_dt, end_dt = _group_window(group)
        print(f"\n[validate] 跑 {group}（{start_dt} → {end_dt}）…")
        result = run_backtest(
            symbols,
            start_dt=start_dt,
            end_dt=end_dt,
            group_name=group,
            k=args.k,
        )
        counts = write_run(result, cfg=cfg, universe_filter=universe_filter, replace=True)
        m = result.metrics
        print(f"  run_id={result.run_id}  trades={m.trade_count}  "
              f"pf={m.profit_factor}  avg_R={m.avg_R}  落库={counts}")
        run_ids.append(result.run_id)

    # 并排对比（复用 analyze_run）
    print("\n" + "=" * 72)
    print("[validate] 跨组分布对比")
    import analyze_run  # noqa: E402  同目录脚本

    con = db.connect_ro("backtest")
    try:
        runs = [analyze_run.load_run(con, rid) for rid in run_ids]
        if len(runs) == 2:
            analyze_run.print_comparison(runs[0], runs[1])
        else:
            for run in runs:
                analyze_run.print_report(run)
    finally:
        con.close()


if __name__ == "__main__":
    main()
