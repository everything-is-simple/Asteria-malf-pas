"""SQLite 连接与建库（WAL 模式）。

分三库：market / malf_pas / backtest，物理文件在外置 DATA_ROOT。
schema.sql 用 ``-- @db: <name>`` 注释把 DDL 分段，建库时只执行对应库的段落。
UI 用 connect_ro() 取只读连接，避免写锁争用。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from config import settings

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_DB_PATHS = {
    "market": settings.MARKET_DB,
    "malf_pas": settings.MALF_PAS_DB,
    "backtest": settings.BACKTEST_DB,
}


def _apply_pragmas(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA foreign_keys=ON;")


def _schema_segments() -> dict[str, str]:
    """按 ``-- @db: <name>`` 标记切分 schema.sql。"""
    text = _SCHEMA_PATH.read_text(encoding="utf-8")
    segments: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        marker = line.strip()
        if marker.startswith("-- @db:"):
            current = marker.split(":", 1)[1].split()[0].strip()
            segments.setdefault(current, [])
            continue
        if current is not None:
            segments[current].append(line)
    return {name: "\n".join(lines) for name, lines in segments.items()}


def connect(db_name: str) -> sqlite3.Connection:
    """打开可写连接（WAL）。db_name ∈ {market, malf_pas, backtest}。"""
    path = _DB_PATHS[db_name]
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    _apply_pragmas(con)
    return con


def connect_ro(db_name: str) -> sqlite3.Connection:
    """打开只读连接（UI 用）。库不存在则报错。"""
    path = _DB_PATHS[db_name]
    if not path.exists():
        raise FileNotFoundError(f"DB not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def init_db(db_name: str) -> None:
    """建库并执行对应段落的 DDL（幂等）。"""
    segments = _schema_segments()
    if db_name not in segments:
        raise KeyError(f"No schema segment for db: {db_name}")
    con = connect(db_name)
    try:
        con.executescript(segments[db_name])
        con.commit()
    finally:
        con.close()


def init_all() -> None:
    settings.ensure_external_dirs()
    for name in _DB_PATHS:
        init_db(name)
