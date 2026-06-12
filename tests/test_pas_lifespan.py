"""PAS Lifespan 四态机测试（M3 验证）。

测试覆盖（PAS_02 L-TR1~5）：
- TR1: observing → active（至少一族 posture ∈ {favored, allowed}）
- TR2: active → submitted（mark_submitted，Signal 接收候选）
- TR3: active → invalidated（posture→blocked / premise 反转 / transition）
- TR4: submitted → invalidated（MALF 结构变化）
- TR5: invalidated → active（新快照 posture 重新满足 → 新 lifespan_id）
- 批量分组处理

🔒 验证边界铁律：
- invalidated 由 MALF/Core 结构事实驱动，不由 Signal 裁决驱动。
- TR5 必须新建 lifespan_id（不复用旧 id）。
"""

from datetime import date

from asteria.malf.types import Direction, SystemState
from asteria.pas.lifespan import LifespanEngine, derive_lifespan_records
from asteria.pas.types import (
    DirectionalPremise,
    LifespanState,
    PASCoreSnapshot,
    Posture,
)


# =========================================================================
# 辅助函数：构造 PASCoreSnapshot
# =========================================================================
def make_snapshot(
    bar_dt: date = date(2025, 1, 10),
    *,
    wave_id: int | None = 1,
    system_state: str | None = SystemState.UP_ALIVE.value,
    premise: DirectionalPremise = DirectionalPremise.EXPECT_STRENGTH_CONTINUATION,
    tst: Posture = Posture.ALLOWED,
    bof: Posture = Posture.BLOCKED,
    bpb: Posture = Posture.FAVORED,
    pb: Posture = Posture.FAVORED,
    cpb: Posture = Posture.DEFERRED,
    transition_bound: bool = False,
    symbol: str = "600000.SH",
    timeframe: str = "day",
) -> PASCoreSnapshot:
    """构造 PASCoreSnapshot 测试数据。默认是一个 actionable（PM-T1）快照。"""
    return PASCoreSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        bar_dt=bar_dt,
        wave_id=wave_id,
        direction=Direction.UP,
        system_state=system_state,
        directional_premise=premise,
        tst_posture=tst,
        bof_posture=bof,
        bpb_posture=bpb,
        pb_posture=pb,
        cpb_posture=cpb,
        transition_bound=transition_bound,
    )


def all_blocked_snapshot(bar_dt: date, **kw) -> PASCoreSnapshot:
    """全 blocked 快照（用于触发 invalidate）。"""
    return make_snapshot(
        bar_dt,
        premise=DirectionalPremise.NO_ACTIONABLE_PREMISE,
        tst=Posture.BLOCKED,
        bof=Posture.BLOCKED,
        bpb=Posture.BLOCKED,
        pb=Posture.BLOCKED,
        cpb=Posture.BLOCKED,
        **kw,
    )


# =========================================================================
# TR1: observing → active
# =========================================================================
def test_tr1_observing_to_active():
    """TR1：有 actionable posture → observing 转 active，分配 lifespan_id。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    assert engine.current_state == LifespanState.OBSERVING
    assert engine.current_lifespan_id is None

    rec = engine.process_snapshot(make_snapshot())
    assert rec.lifespan_state == LifespanState.ACTIVE
    assert engine.current_lifespan_id is not None
    assert rec.lifespan_id == engine.current_lifespan_id
    assert "TR1" in rec.transition_reason


def test_tr1_stays_observing_when_no_actionable():
    """无 actionable posture（全 blocked）→ 保持 observing。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    rec = engine.process_snapshot(all_blocked_snapshot(date(2025, 1, 10)))
    assert rec.lifespan_state == LifespanState.OBSERVING
    assert engine.current_lifespan_id is None


# =========================================================================
# TR2: active → submitted（mark_submitted）
# =========================================================================
def test_tr2_active_to_submitted():
    """TR2：active 态调用 mark_submitted → submitted，lifespan_id 不变。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    engine.process_snapshot(make_snapshot(date(2025, 1, 10)))
    lid = engine.current_lifespan_id

    engine.mark_submitted(date(2025, 1, 11))
    assert engine.current_state == LifespanState.SUBMITTED
    assert engine.current_lifespan_id == lid  # id 不变
    # 补了一条 submitted record
    assert engine.records[-1].lifespan_state == LifespanState.SUBMITTED
    assert "TR2" in engine.records[-1].transition_reason


def test_tr2_no_effect_when_not_active():
    """observing 态调用 mark_submitted → 无效（保持 observing）。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    engine.mark_submitted(date(2025, 1, 11))
    assert engine.current_state == LifespanState.OBSERVING


# =========================================================================
# TR3: active → invalidated（MALF 结构驱动）
# =========================================================================
def test_tr3_active_to_invalidated_all_blocked():
    """TR3：active 态遇全 blocked → invalidated。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    engine.process_snapshot(make_snapshot(date(2025, 1, 10)))
    rec = engine.process_snapshot(all_blocked_snapshot(date(2025, 1, 11)))
    assert rec.lifespan_state == LifespanState.INVALIDATED
    assert "TR3" in rec.transition_reason


def test_tr3_active_to_invalidated_transition():
    """TR3：active 态遇 MALF transition → invalidated（结构驱动）。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    engine.process_snapshot(make_snapshot(date(2025, 1, 10)))
    rec = engine.process_snapshot(
        make_snapshot(date(2025, 1, 11), system_state=SystemState.TRANSITION.value)
    )
    assert rec.lifespan_state == LifespanState.INVALIDATED


def test_tr3_active_to_invalidated_premise_reversal():
    """TR3：active 态遇 premise 反转（weakness_rejection）→ invalidated。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    engine.process_snapshot(make_snapshot(date(2025, 1, 10)))
    rec = engine.process_snapshot(
        make_snapshot(
            date(2025, 1, 11),
            premise=DirectionalPremise.EXPECT_WEAKNESS_REJECTION,
        )
    )
    assert rec.lifespan_state == LifespanState.INVALIDATED


def test_tr3_active_stays_when_still_actionable():
    """active 态且 posture 仍 actionable、无结构变化 → 保持 active。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    engine.process_snapshot(make_snapshot(date(2025, 1, 10)))
    rec = engine.process_snapshot(make_snapshot(date(2025, 1, 11)))
    assert rec.lifespan_state == LifespanState.ACTIVE


# =========================================================================
# TR4: submitted → invalidated（MALF 结构驱动，非 Signal 裁决）
# =========================================================================
def test_tr4_submitted_to_invalidated():
    """TR4：submitted 态遇 MALF 结构变化 → invalidated。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    engine.process_snapshot(make_snapshot(date(2025, 1, 10)))
    engine.mark_submitted(date(2025, 1, 11))
    assert engine.current_state == LifespanState.SUBMITTED

    rec = engine.process_snapshot(all_blocked_snapshot(date(2025, 1, 12)))
    assert rec.lifespan_state == LifespanState.INVALIDATED
    assert "TR4" in rec.transition_reason


def test_tr4_submitted_stays_when_no_structure_change():
    """submitted 态且无结构变化 → 保持 submitted（Signal 裁决不驱动状态）。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    engine.process_snapshot(make_snapshot(date(2025, 1, 10)))
    engine.mark_submitted(date(2025, 1, 11))
    rec = engine.process_snapshot(make_snapshot(date(2025, 1, 12)))
    assert rec.lifespan_state == LifespanState.SUBMITTED


# =========================================================================
# TR5: invalidated → active（新 lifespan_id）
# =========================================================================
def test_tr5_invalidated_to_active_new_lifespan_id():
    """TR5：invalidated 态 posture 重新满足 → active，且分配新 lifespan_id。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    engine.process_snapshot(make_snapshot(date(2025, 1, 10)))
    old_lid = engine.current_lifespan_id
    # 触发 invalidate
    engine.process_snapshot(all_blocked_snapshot(date(2025, 1, 11)))
    assert engine.current_state == LifespanState.INVALIDATED
    # posture 恢复 → 新 lifespan
    rec = engine.process_snapshot(make_snapshot(date(2025, 1, 12)))
    assert rec.lifespan_state == LifespanState.ACTIVE
    assert engine.current_lifespan_id != old_lid  # 🔒 新 id，不复用
    assert "TR5" in rec.transition_reason


def test_tr5_stays_invalidated_when_no_actionable():
    """invalidated 态仍无 actionable posture → 保持 invalidated。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    engine.process_snapshot(make_snapshot(date(2025, 1, 10)))
    engine.process_snapshot(all_blocked_snapshot(date(2025, 1, 11)))
    rec = engine.process_snapshot(all_blocked_snapshot(date(2025, 1, 12)))
    assert rec.lifespan_state == LifespanState.INVALIDATED
    assert "waiting" in rec.transition_reason


# =========================================================================
# 完整生命周期 + 批量处理
# =========================================================================
def test_full_lifecycle_sequence():
    """完整序列：observing → active → invalidated → active(new id)。"""
    engine = LifespanEngine(symbol="600000.SH", timeframe="day")
    states = []
    snaps = [
        all_blocked_snapshot(date(2025, 1, 10)),  # observing
        make_snapshot(date(2025, 1, 11)),          # active
        make_snapshot(date(2025, 1, 12)),          # active (stay)
        all_blocked_snapshot(date(2025, 1, 13)),   # invalidated
        make_snapshot(date(2025, 1, 14)),          # active (new lifespan)
    ]
    for s in snaps:
        states.append(engine.process_snapshot(s).lifespan_state)
    assert states == [
        LifespanState.OBSERVING,
        LifespanState.ACTIVE,
        LifespanState.ACTIVE,
        LifespanState.INVALIDATED,
        LifespanState.ACTIVE,
    ]


def test_derive_lifespan_records_groups_by_symbol():
    """批量派生：按 symbol+timeframe 分组，各自独立状态机。"""
    snaps = [
        make_snapshot(date(2025, 1, 10), symbol="600000.SH"),
        make_snapshot(date(2025, 1, 10), symbol="000001.SZ"),
        make_snapshot(date(2025, 1, 11), symbol="600000.SH"),
        make_snapshot(date(2025, 1, 11), symbol="000001.SZ"),
    ]
    records = derive_lifespan_records(snaps)
    assert len(records) == 4
    sh = [r for r in records if r.symbol == "600000.SH"]
    sz = [r for r in records if r.symbol == "000001.SZ"]
    assert len(sh) == 2 and len(sz) == 2
    # 各组首条都从 observing 转 active
    assert sh[0].lifespan_state == LifespanState.ACTIVE
    assert sz[0].lifespan_state == LifespanState.ACTIVE
    # 两组 lifespan_id 互不相同
    assert sh[0].lifespan_id != sz[0].lifespan_id
