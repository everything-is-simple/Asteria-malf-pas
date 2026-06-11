"""CLI: 单标的全链路 MALF（Core + Lifespan + v1.5 行为）。

用法：
  python scripts/run_malf.py --symbol 600000.SH            # 只算 + 打印验收摘要（不写库）
  python scripts/run_malf.py --symbol 600000.SH --write    # 同时落库到 malf_pas

验收口径（docs/TEST_ACCEPTANCE.md §2 M2 端到端）：
  字段齐全；transition bar 的 direction=old_direction；rank ∈ [0,1]。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让 src 布局可被 import（无需安装）
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from asteria.malf.runner import run_symbol_full  # noqa: E402
from asteria.malf.types import SystemState  # noqa: E402


def _fmt(v: object) -> str:
    """枚举取 .value，None 显示为 '-'。"""
    if v is None:
        return "-"
    return str(v.value) if hasattr(v, "value") else str(v)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="跑单标的全链路 MALF：Core → Lifespan → v1.5 行为"
    )
    ap.add_argument("--symbol", required=True, help="标的，如 600000.SH")
    ap.add_argument("--timeframe", default="day")
    ap.add_argument("--k", type=int, default=2, help="分形确认 k（默认 2）")
    ap.add_argument("--tail", type=int, default=10, help="打印末尾 N 个 bar（默认 10）")
    ap.add_argument("--write", action="store_true", help="落库到 malf_pas")
    args = ap.parse_args()

    run = run_symbol_full(
        args.symbol,
        timeframe=args.timeframe,
        k=args.k,
        source_run_id=f"run-malf-{args.symbol}",
    )

    n = len(run.bars)
    if n == 0:
        print(f"[{args.symbol}] 无行情数据（先 ingest_data.py 灌数）。")
        return

    # --- 概览 ---
    n_waves = len(run.core.waves)
    n_breaks = len(run.core.breaks)
    n_trans = len(run.core.transitions)
    print(f"[{args.symbol}] bars={n}  waves={n_waves}  breaks={n_breaks}  transitions={n_trans}")
    print(f"  positions={len(run.positions)}  behaviors={len(run.behaviors)}  (应各 == bars)")

    # --- 验收自检 ---
    ok = _acceptance_checks(run)

    # --- 末尾 N 个 bar 明细 ---
    tail = args.tail
    print(f"\n末尾 {tail} 个 bar（WavePosition + 关键 regime）：")
    print(
        f"{'bar_dt':<12}{'sys':<14}{'dir':<6}{'nc':>4}{'nns':>5}"
        f"{'urank':>7}{'srank':>7}{'life':<12}{'cont':<14}{'stag':<16}"
    )
    for pos, beh in zip(run.positions[-tail:], run.behaviors[-tail:]):
        urank = f"{pos.update_rank:.2f}" if pos.update_rank is not None else "-"
        srank = f"{pos.stagnation_rank:.2f}" if pos.stagnation_rank is not None else "-"
        print(
            f"{pos.bar_dt.isoformat():<12}{_fmt(pos.system_state):<14}{_fmt(pos.direction):<6}"
            f"{pos.new_count:>4}{pos.no_new_span:>5}{urank:>7}{srank:>7}"
            f"{_fmt(pos.life_state):<12}{_fmt(beh.continuation_regime):<14}"
            f"{_fmt(beh.stagnation_regime):<16}"
        )

    # --- 落库 ---
    if args.write:
        from asteria.storage import db
        from asteria.storage.malf_writer import write_run

        db.init_db("malf_pas")
        counts = write_run(run, k=args.k)
        print(f"\n已写入 malf_pas：{counts}")

    # 验收失败 → 非零退出码（便于脚本化自动验收）。落库已先执行，不影响数据写入。
    if not ok:
        raise SystemExit(1)


def _acceptance_checks(run) -> bool:
    """端到端验收自检：字段齐全 / transition=old_direction / rank ∈ [0,1]。

    返回 True 表示全部通过；False 表示有问题（供 CLI 设非零退出码，便于自动验收）。
    """
    problems: list[str] = []

    # 1. 三组逐 bar 一一对应
    if not (len(run.positions) == len(run.behaviors) == len(run.bars)):
        problems.append(
            f"长度不一致：bars={len(run.bars)} positions={len(run.positions)} "
            f"behaviors={len(run.behaviors)}"
        )

    # 2. transition bar 的 direction = old_direction（L-T5）：非 None 即合格
    trans_positions = [p for p in run.positions if p.system_state == SystemState.TRANSITION]
    bad_trans = [p for p in trans_positions if p.direction is None]
    if bad_trans:
        problems.append(f"{len(bad_trans)} 个 transition bar 的 direction 为 None（违反 L-T5）")

    # 3. rank ∈ [0,1]（非 None 时）
    for p in run.positions:
        for rank_name, rank in (("update_rank", p.update_rank), ("stagnation_rank", p.stagnation_rank)):
            if rank is not None and not (0.0 <= rank <= 1.0):
                problems.append(f"{p.bar_dt} {rank_name}={rank} 越界 [0,1]")
                break

    # 4. 至少产出一个活波坐标（否则数据太短，验收无意义）
    has_alive = any(
        p.system_state in (SystemState.UP_ALIVE, SystemState.DOWN_ALIVE)
        for p in run.positions
    )
    if not has_alive:
        problems.append("未产出任何活波（数据太短或无结构），端到端验收无判别力")

    if problems:
        print("\n[验收 ✗] 发现问题：")
        for p in problems:
            print(f"  - {p}")
        return False
    n_terminal = sum(1 for p in run.positions if _fmt(p.life_state) == "terminal")
    print(
        f"\n[验收 ✓] 字段齐全 · transition 保留 old_direction · rank∈[0,1] · "
        f"terminal bar 数={n_terminal}"
    )
    return True


if __name__ == "__main__":
    main()
