"""MALF 快照持久化：把 MalfRunResult 写入 malf_pas 库（幂等）。

写三张主快照表（schema.sql 已建，M1）：
- ``malf_core_snapshot``      逐 bar 结构状态（O7）
- ``wave_position``           逐 bar 生命统计（L18）
- ``wave_behavior_snapshot``  逐 bar v1.5 行为（MALF_03）

幂等：三表均有 ``UNIQUE(symbol, timeframe, bar_dt, source_run_id)``，
用 ``INSERT OR REPLACE`` 保证同一 run 重跑不产生重复行（append-only 语义下，
同 (symbol,timeframe,bar_dt,run) 是同一逻辑事实，覆盖即幂等）。

不在此写 pivot/wave/break/transition 事件账本（那是可视化叠加，非 PAS 输入）；
本 writer 只负责 PAS 的唯一输入面（WavePosition + WaveBehaviorSnapshot）+ Core 快照。
"""

from __future__ import annotations

import json
import sqlite3

from asteria.malf.behavior import BEHAVIOR_RULE_VERSION
from asteria.malf.core import CORE_RULE_VERSION
from asteria.malf.pivot import rule_version as pivot_rule_version
from asteria.malf.runner import MalfRunResult
from asteria.malf.types import (
    CoreStateSnapshot,
    WaveBehaviorSnapshot,
    WavePosition,
)
from asteria.storage import db


def _enum_val(v: object) -> object:
    """枚举取 .value，其余原样（None 保持 None）。"""
    return v.value if hasattr(v, "value") else v


def _core_row(snap: CoreStateSnapshot, *, k: int, source_run_id: str) -> tuple:
    return (
        snap.symbol,
        snap.timeframe,
        snap.bar_dt.isoformat(),
        _enum_val(snap.system_state),
        snap.active_wave_id,
        snap.old_wave_id,
        _enum_val(snap.direction),
        _enum_val(snap.wave_core_state),
        snap.current_effective_guard_pivot_id,
        snap.current_effective_guard_price,
        snap.progress_extreme_pivot_id,
        snap.progress_extreme_price,
        snap.open_transition_id,
        snap.active_candidate_guard_pivot_id,
        _enum_val(snap.active_candidate_direction),
        snap.transition_boundary_high,
        snap.transition_boundary_low,
        CORE_RULE_VERSION,
        pivot_rule_version(k),
        source_run_id,
    )


_CORE_SQL = """
INSERT OR REPLACE INTO malf_core_snapshot (
    symbol, timeframe, bar_dt, system_state, active_wave_id, old_wave_id,
    direction, wave_core_state, current_effective_guard_pivot_id,
    current_effective_guard_price, progress_extreme_pivot_id, progress_extreme_price,
    open_transition_id, active_candidate_guard_pivot_id, active_candidate_direction,
    transition_boundary_high, transition_boundary_low,
    core_rule_version, pivot_detection_rule_version, source_run_id
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _position_row(pos: WavePosition) -> tuple:
    return (
        pos.symbol,
        pos.timeframe,
        pos.bar_dt.isoformat(),
        pos.wave_id,
        pos.old_wave_id,
        _enum_val(pos.system_state),
        _enum_val(pos.wave_core_state),
        _enum_val(pos.direction),
        pos.new_count,
        pos.no_new_span,
        pos.transition_span,
        pos.update_rank,
        pos.stagnation_rank,
        _enum_val(pos.life_state),
        _enum_val(pos.position_quadrant),
        _enum_val(pos.birth_type),
        pos.candidate_wait_span,
        pos.candidate_replacement_count,
        pos.confirmation_distance_abs,
        pos.confirmation_distance_pct,
        pos.sample_version,
        pos.lifespan_rule_version,
        pos.source_run_id,
    )


_POSITION_SQL = """
INSERT OR REPLACE INTO wave_position (
    symbol, timeframe, bar_dt, wave_id, old_wave_id, system_state, wave_core_state,
    direction, new_count, no_new_span, transition_span, update_rank, stagnation_rank,
    life_state, position_quadrant, birth_type, candidate_wait_span,
    candidate_replacement_count, confirmation_distance_abs, confirmation_distance_pct,
    sample_version, lifespan_rule_version, source_run_id
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _behavior_row(beh: WaveBehaviorSnapshot) -> tuple:
    return (
        beh.symbol,
        beh.timeframe,
        beh.bar_dt.isoformat(),
        beh.wave_id,
        _enum_val(beh.direction),
        beh.old_wave_id,
        beh.open_transition_id,
        _enum_val(beh.continuation_regime),
        _enum_val(beh.directional_continuity_regime),
        _enum_val(beh.stagnation_regime),
        _enum_val(beh.boundary_pressure_regime),
        _enum_val(beh.transition_regime),
        _enum_val(beh.birth_quality_regime),
        json.dumps(beh.reason_codes, ensure_ascii=False),
        beh.lineage_hash,
        beh.malf_v1_4_rule_version or CORE_RULE_VERSION,
        beh.malf_v1_5_rule_version or BEHAVIOR_RULE_VERSION,
        beh.source_run_id,
    )


_BEHAVIOR_SQL = """
INSERT OR REPLACE INTO wave_behavior_snapshot (
    symbol, timeframe, bar_dt, wave_id, direction, old_wave_id, open_transition_id,
    continuation_regime, directional_continuity_regime, stagnation_regime,
    boundary_pressure_regime, transition_regime, birth_quality_regime,
    reason_codes, lineage_hash, malf_v1_4_rule_version, malf_v1_5_rule_version,
    source_run_id
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def write_run(
    run: MalfRunResult,
    *,
    k: int = 2,
    con: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """把全链路结果写入 malf_pas 库。返回各表写入行数。

    幂等：同 (symbol,timeframe,bar_dt,source_run_id) 覆盖。调用方需保证库已建
    （db.init_db("malf_pas")）。
    """
    own = con is None
    con = con or db.connect("malf_pas")
    try:
        source_run_id = run.positions[0].source_run_id if run.positions else "adhoc"
        core_rows = [_core_row(s, k=k, source_run_id=source_run_id) for s in run.core.snapshots]
        pos_rows = [_position_row(p) for p in run.positions]
        beh_rows = [_behavior_row(b) for b in run.behaviors]
        con.executemany(_CORE_SQL, core_rows)
        con.executemany(_POSITION_SQL, pos_rows)
        con.executemany(_BEHAVIOR_SQL, beh_rows)
        con.commit()
        return {
            "core_snapshots": len(core_rows),
            "wave_positions": len(pos_rows),
            "behavior_snapshots": len(beh_rows),
        }
    finally:
        if own:
            con.close()
