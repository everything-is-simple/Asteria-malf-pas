"""MALF 端到端 smoke 测试：assemble_from_bars 全链路 + writer 落库往返。

不依赖真实行情库：用合成 OHLC 序列走 Core→Lifespan→behavior，
再用临时 sqlite（从 schema.sql 抽 malf_pas 段建表）验证 writer 写入 + 读回一致。

验收点（docs/03-task-breakdown/TEST_ACCEPTANCE.md §2 M2 端到端）：
- positions / behaviors 逐 bar 与 bars 一一对应。
- transition bar 的 direction = old_direction（非 None）。
- rank ∈ [0,1]。
- writer 三表写入行数 == bars 数，且读回字段一致。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from asteria.data.contracts import DailyBar  # noqa: E402
from asteria.malf.runner import assemble_from_bars  # noqa: E402
from asteria.malf.types import LifeState, SystemState, WaveCoreState  # noqa: E402
from asteria.storage.malf_writer import write_run  # noqa: E402

_D0 = date(2020, 1, 1)


def _bar(i: int, high: float, low: float) -> DailyBar:
    mid = round((high + low) / 2, 2)
    return DailyBar(
        symbol="TEST",
        bar_dt=_D0 + timedelta(days=i),
        open=mid,
        high=high,
        low=low,
        close=mid,
        volume=1.0,
        amount=1.0,
    )


# up wave → break → 反向 down wave 确认 → 多次推进（含 transition + terminal + birth 距离）
_SEQ = [
    (10.0, 9.0),
    (12.0, 11.0),
    (11.5, 10.0),
    (11.0, 8.0),
    (11.8, 9.5),
    (14.0, 12.5),
    (13.0, 12.0),
    (13.5, 7.5),
    (12.0, 9.0),
    (13.0, 9.5),
    (10.0, 5.0),
    (9.0, 6.5),
    (8.0, 3.0),
    (7.0, 4.5),
    (6.0, 2.0),
    (5.5, 3.5),
]


def _bars() -> list[DailyBar]:
    return [_bar(i, hi, lo) for i, (hi, lo) in enumerate(_SEQ)]


def _malf_pas_schema() -> str:
    """从 schema.sql 抽取 malf_pas 段（-- @db: malf_pas 到下一个 @db 标记）。"""
    schema_path = _SRC / "asteria" / "storage" / "schema.sql"
    text = schema_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    current: str | None = None
    for line in lines:
        marker = line.strip()
        if marker.startswith("-- @db:"):
            current = marker.split(":", 1)[1].split()[0].strip()
            continue
        if current == "malf_pas":
            out.append(line)
    return "\n".join(out)


def test_assemble_from_bars_e2e_fields_and_invariants():
    """全链路组装：三组逐 bar 对应 + transition=old_direction + rank∈[0,1]。"""
    bars = _bars()
    run = assemble_from_bars(bars, symbol="TEST", k=1, source_run_id="e2e-test")

    # 三组逐 bar 一一对应
    assert len(run.positions) == len(run.behaviors) == len(bars)
    assert [p.bar_dt for p in run.positions] == [b.bar_dt for b in bars]
    assert [b.bar_dt for b in run.behaviors] == [b.bar_dt for b in bars]

    # transition bar 的 direction 非 None（L-T5 保留 old_direction）
    trans = [p for p in run.positions if p.system_state == SystemState.TRANSITION]
    assert trans, "序列应产生 transition"
    for p in trans:
        assert p.direction is not None

    # rank ∈ [0,1]
    for p in run.positions:
        for rank in (p.update_rank, p.stagnation_rank):
            if rank is not None:
                assert 0.0 <= rank <= 1.0

    # 至少一个活波（否则数据太短，验收无判别力）
    assert any(
        p.system_state in (SystemState.UP_ALIVE, SystemState.DOWN_ALIVE)
        for p in run.positions
    )
    # 至少一个 terminal（P3 真实路径：break bar 标记 terminated → life_state=terminal）
    terminal = [
        p for p in run.positions
        if p.wave_core_state == WaveCoreState.TERMINATED
        and p.life_state == LifeState.TERMINAL
    ]
    assert terminal, "未产出 terminal bar（P3 真实路径未覆盖）"
    # P3：break bar 真实产出 terminal（wave_core_state=terminated → life_state=terminal）
    assert any(p.wave_core_state == WaveCoreState.TERMINATED for p in run.positions)
    assert any(p.life_state == LifeState.TERMINAL for p in run.positions)


def test_writer_roundtrip(tmp_path):
    """writer 落库往返：三表写入行数 == bars，读回关键字段一致。"""
    bars = _bars()
    run = assemble_from_bars(bars, symbol="TEST", k=1, source_run_id="e2e-test")

    # 临时库建表（只建 malf_pas 段）
    db_path = tmp_path / "malf_pas_test.sqlite"
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.executescript(_malf_pas_schema())
    con.commit()

    counts = write_run(run, k=1, con=con)
    assert counts["core_snapshots"] == len(bars)
    assert counts["wave_positions"] == len(bars)
    assert counts["behavior_snapshots"] == len(bars)

    # 读回 wave_position 行数与字段
    rows = con.execute(
        "SELECT * FROM wave_position WHERE symbol='TEST' ORDER BY bar_dt"
    ).fetchall()
    assert len(rows) == len(bars)
    # 逐 bar 比对 new_count / direction 与内存一致
    for r, pos in zip(rows, run.positions):
        assert r["new_count"] == pos.new_count
        assert r["no_new_span"] == pos.no_new_span
        expected_dir = pos.direction.value if pos.direction is not None else None
        assert r["direction"] == expected_dir

    # behavior 表读回行数
    beh_rows = con.execute(
        "SELECT * FROM wave_behavior_snapshot WHERE symbol='TEST'"
    ).fetchall()
    assert len(beh_rows) == len(bars)
    con.close()


def test_writer_idempotent(tmp_path):
    """同一 run 重复写入：INSERT OR REPLACE 幂等，行数不翻倍。"""
    bars = _bars()
    run = assemble_from_bars(bars, symbol="TEST", k=1, source_run_id="e2e-test")

    db_path = tmp_path / "malf_pas_test.sqlite"
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.executescript(_malf_pas_schema())
    con.commit()

    write_run(run, k=1, con=con)
    write_run(run, k=1, con=con)  # 重写一次

    n = con.execute(
        "SELECT COUNT(*) AS c FROM wave_position WHERE symbol='TEST'"
    ).fetchone()["c"]
    assert n == len(bars), f"幂等失败：重写后 wave_position 行数={n}，应={len(bars)}"
    con.close()
