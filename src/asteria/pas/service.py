"""PAS v1.5 Service 只读发布接口（PAS_04）。

提供只读查询接口，不含 accept/reject 决策逻辑。

发布内容：
- PASCoreSnapshot(Latest)
- PASCandidateRecord(Latest)
- PASLifespanRecord
- PASServiceHandoffRecord

🔒 边界铁律：
- 只读发布，不做 accept/reject。
- SignalFeedback 只用于 audit/统计/replay，绝不回写 Core/Lifespan/MALF。
"""

from __future__ import annotations

from datetime import date

from asteria.pas.types import (
    PASCandidateRecord,
    PASCoreSnapshot,
    PASLifespanRecord,
    PASServiceHandoffRecord,
    Posture,
)


# =========================================================================
# 只读查询接口
# =========================================================================
def get_latest_snapshot(
    snapshots: list[PASCoreSnapshot], symbol: str, timeframe: str
) -> PASCoreSnapshot | None:
    """获取指定标的最新 PASCoreSnapshot。"""
    filtered = [s for s in snapshots if s.symbol == symbol and s.timeframe == timeframe]
    if not filtered:
        return None
    return max(filtered, key=lambda s: s.bar_dt)


def get_latest_candidate(
    snapshots: list[PASCoreSnapshot], symbol: str, timeframe: str
) -> PASCandidateRecord | None:
    """获取指定标的最新候选记录（至少一族 posture ∈ {favored, allowed}）。"""
    snap = get_latest_snapshot(snapshots, symbol, timeframe)
    if not snap:
        return None
    if not _has_actionable_posture(snap):
        return None
    return PASCandidateRecord(
        symbol=snap.symbol,
        timeframe=snap.timeframe,
        bar_dt=snap.bar_dt,
        wave_id=snap.wave_id,
        directional_premise=snap.directional_premise,
        read_status=snap.read_status,
        tst_posture=snap.tst_posture,
        bof_posture=snap.bof_posture,
        bpb_posture=snap.bpb_posture,
        pb_posture=snap.pb_posture,
        cpb_posture=snap.cpb_posture,
        source_run_id=snap.source_run_id,
    )


def get_all_active_candidates(
    snapshots: list[PASCoreSnapshot],
) -> list[PASCandidateRecord]:
    """获取所有 active 候选（每个 symbol+timeframe 最新一条，且有 actionable posture）。"""
    # 按 symbol+timeframe 分组，取最新
    groups: dict[tuple[str, str], PASCoreSnapshot] = {}
    for snap in snapshots:
        key = (snap.symbol, snap.timeframe)
        if key not in groups or snap.bar_dt > groups[key].bar_dt:
            groups[key] = snap

    # 筛选 actionable
    candidates = []
    for snap in groups.values():
        if _has_actionable_posture(snap):
            candidates.append(
                PASCandidateRecord(
                    symbol=snap.symbol,
                    timeframe=snap.timeframe,
                    bar_dt=snap.bar_dt,
                    wave_id=snap.wave_id,
                    directional_premise=snap.directional_premise,
                    read_status=snap.read_status,
                    tst_posture=snap.tst_posture,
                    bof_posture=snap.bof_posture,
                    bpb_posture=snap.bpb_posture,
                    pb_posture=snap.pb_posture,
                    cpb_posture=snap.cpb_posture,
                    source_run_id=snap.source_run_id,
                )
            )
    return candidates


def get_lifespan_records(
    records: list[PASLifespanRecord], symbol: str, timeframe: str
) -> list[PASLifespanRecord]:
    """获取指定标的所有 lifespan 记录（时间序列）。"""
    return [r for r in records if r.symbol == symbol and r.timeframe == timeframe]


def create_handoff_record(
    snap: PASCoreSnapshot,
    lifespan_id: str,
    handoff_dt: date,
    source_run_id: str = "service",
) -> PASServiceHandoffRecord:
    """创建 PAS → Signal 切换记录（TR2）。"""
    return PASServiceHandoffRecord(
        symbol=snap.symbol,
        timeframe=snap.timeframe,
        bar_dt=snap.bar_dt,
        lifespan_id=lifespan_id,
        wave_id=snap.wave_id,
        handoff_dt=handoff_dt,
        source_run_id=source_run_id,
    )


# =========================================================================
# 辅助函数
# =========================================================================
def _has_actionable_posture(snap: PASCoreSnapshot) -> bool:
    """至少一族 posture ∈ {favored, allowed}。"""
    return any(
        p in (Posture.FAVORED, Posture.ALLOWED)
        for p in [
            snap.tst_posture,
            snap.bof_posture,
            snap.bpb_posture,
            snap.pb_posture,
            snap.cpb_posture,
        ]
    )
