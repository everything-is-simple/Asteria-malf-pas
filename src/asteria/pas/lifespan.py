"""PAS v1.5 Lifespan 四态机（PAS_02 L-TR1~5）。

管理 PAS opportunity 生命周期：observing → active → submitted/invalidated → observing。

🔒 边界铁律：
- invalidated 由 MALF/Core 驱动（guard broken / transition），不由 Signal 裁决驱动。
- submitted ≠ accepted，只表示 Signal 接收了候选。
- 状态转换纯粹基于 MALF 结构事实 + PAS posture，不回写 Core。

状态转换（L-TR1~5）：
  TR1: observing → active（至少一族 posture ∈ {favored, allowed}）
  TR2: active → submitted（Signal 接收候选 + HandoffRecord 已记）
  TR3: active → invalidated（posture→blocked / premise 反转 / MALF guard broken 或 transition）
  TR4: submitted → invalidated（Signal rejected 且同时触发 TR3）
  TR5: invalidated → observing（新快照 posture 重新满足 → 新建 lifespan_id）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from asteria.malf.types import SystemState
from asteria.pas.types import (
    LifespanState,
    PASCoreSnapshot,
    PASLifespanRecord,
    Posture,
)


@dataclass
class LifespanEngine:
    """PAS Lifespan 四态机（有状态）。

    每个 symbol+timeframe 维护一个 engine 实例。
    """

    symbol: str
    timeframe: str
    # 当前状态
    current_lifespan_id: str | None = None
    current_state: LifespanState = LifespanState.OBSERVING
    current_wave_id: int | None = None
    # 历史记录（append-only）
    records: list[PASLifespanRecord] | None = None

    def __post_init__(self):
        if self.records is None:
            self.records = []

    def process_snapshot(
        self, snap: PASCoreSnapshot, source_run_id: str = "adhoc"
    ) -> PASLifespanRecord:
        """逐 bar 处理 PASCoreSnapshot → 更新状态 → 发布 PASLifespanRecord。"""
        reason = ""

        # TR1: observing → active（至少一族 posture ∈ {favored, allowed}）
        if self.current_state == LifespanState.OBSERVING:
            if self._has_actionable_posture(snap):
                self.current_lifespan_id = str(uuid.uuid4())
                self.current_state = LifespanState.ACTIVE
                self.current_wave_id = snap.wave_id
                reason = "TR1:observing→active(posture∈{favored,allowed})"

        # TR3: active → invalidated（posture→blocked / premise 反转 / MALF guard broken 或 transition）
        elif self.current_state == LifespanState.ACTIVE:
            if self._should_invalidate(snap):
                self.current_state = LifespanState.INVALIDATED
                reason = "TR3:active→invalidated(MALF_structure_change)"

        # TR4: submitted → invalidated（Signal rejected 且同时触发 TR3）
        # 注：Signal rejected 事件不在此处理（由 service 层记录 SignalFeedback，不回写状态）
        # 此处只响应 MALF 结构变化触发的 TR3 条件
        elif self.current_state == LifespanState.SUBMITTED:
            if self._should_invalidate(snap):
                self.current_state = LifespanState.INVALIDATED
                reason = "TR4:submitted→invalidated(MALF_structure_change)"

        # TR5: invalidated → observing（新快照 posture 重新满足 → 新建 lifespan_id）
        elif self.current_state == LifespanState.INVALIDATED:
            if self._has_actionable_posture(snap):
                self.current_lifespan_id = str(uuid.uuid4())
                self.current_state = LifespanState.ACTIVE
                self.current_wave_id = snap.wave_id
                reason = "TR5:invalidated→observing→active(new_lifespan)"
            else:
                # 保持 invalidated，等待 posture 恢复
                reason = "stay:invalidated(waiting_posture)"

        # 生成 record
        record = PASLifespanRecord(
            symbol=snap.symbol,
            timeframe=snap.timeframe,
            bar_dt=snap.bar_dt,
            lifespan_id=self.current_lifespan_id or "none",
            lifespan_state=self.current_state,
            transition_reason=reason,
            wave_id=snap.wave_id,
            pas_snapshot_bar_dt=snap.bar_dt,
            source_run_id=source_run_id,
        )
        self.records.append(record)
        return record

    def mark_submitted(self, bar_dt, reason: str = "TR2:Signal_received"):
        """TR2: active → submitted（Signal 接收候选）。

        由 service 层调用（非 process_snapshot 自动触发）。
        """
        if self.current_state == LifespanState.ACTIVE:
            self.current_state = LifespanState.SUBMITTED
            # 补一条 record
            record = PASLifespanRecord(
                symbol=self.symbol,
                timeframe=self.timeframe,
                bar_dt=bar_dt,
                lifespan_id=self.current_lifespan_id or "none",
                lifespan_state=LifespanState.SUBMITTED,
                transition_reason=reason,
                wave_id=self.current_wave_id,
                pas_snapshot_bar_dt=bar_dt,
                source_run_id="service",
            )
            self.records.append(record)

    def _has_actionable_posture(self, snap: PASCoreSnapshot) -> bool:
        """TR1/TR5 条件：至少一族 posture ∈ {favored, allowed}。"""
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

    def _should_invalidate(self, snap: PASCoreSnapshot) -> bool:
        """TR3/TR4 条件：posture→blocked / premise 反转 / MALF guard broken 或 transition。"""
        # 全 blocked
        all_blocked = all(
            p == Posture.BLOCKED
            for p in [
                snap.tst_posture,
                snap.bof_posture,
                snap.bpb_posture,
                snap.pb_posture,
                snap.cpb_posture,
            ]
        )
        if all_blocked:
            return True
        # MALF transition
        if snap.system_state == SystemState.TRANSITION.value:
            return True
        # premise 反转（expect_weakness_rejection）
        from asteria.pas.types import DirectionalPremise

        if snap.directional_premise == DirectionalPremise.EXPECT_WEAKNESS_REJECTION:
            return True
        # guard broken（transition_bound 标记）
        if snap.transition_bound:
            return True
        return False


# =========================================================================
# 批量处理
# =========================================================================
def derive_lifespan_records(
    snapshots: list[PASCoreSnapshot],
    *,
    source_run_id: str = "adhoc",
) -> list[PASLifespanRecord]:
    """逐 bar 派生 PASLifespanRecord（按 symbol+timeframe 分组，串行处理）。"""
    # 按 symbol+timeframe 分组
    groups: dict[tuple[str, str], list[PASCoreSnapshot]] = {}
    for snap in snapshots:
        key = (snap.symbol, snap.timeframe)
        groups.setdefault(key, []).append(snap)

    all_records: list[PASLifespanRecord] = []
    for (symbol, timeframe), group in groups.items():
        engine = LifespanEngine(symbol=symbol, timeframe=timeframe)
        for snap in group:
            record = engine.process_snapshot(snap, source_run_id)
            all_records.append(record)

    return all_records
