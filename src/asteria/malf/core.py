"""MALF-Core 结构状态机（v1.4 语义 + O1-O8 操作边界）。

逐 bar 推进，事件顺序固定（O2）：
  1. ingest bar
  2. confirm pivots（本 bar confirm_bar_dt 的 pivot）
  3. update active wave progress / current_effective_guard
  4. evaluate break（O3 严格 < / >）
  5. if break: terminate old wave, open transition（D13 双边界）
  6. update active candidate guard（O4 latest 替换）
  7. evaluate progress confirmation（D16 严格突破 boundary）
  8. create new wave if confirmed（T6 双条件）
  9. publish snapshot（O7）

关键裁决：
- break 逐 bar 用 bar.low/high 评估；极值 bar 早于确认 bar k 根，故 break 天然先于
  「违反 guard 的低点 pivot 确认」触发（causal，无未来函数）。
- transition 内处理新 pivot P：先判 P 是否确认现有 active candidate（D16 after），
  不确认才让 P 成为新的 active candidate（T5 flip-flop）。解开 O2 step6/7 表面矛盾。
- 价格比较前 round 到 PRICE_DP 位（O3 epsilon=none_after_normalization）。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from asteria.data.contracts import DailyBar
from asteria.malf.pivot import RawPivot, detect_pivots_incremental, normalize_price, rule_version
from asteria.malf.types import (
    Break,
    CoreStateSnapshot,
    Direction,
    Pivot,
    PivotKind,
    Primitive,
    SystemState,
    Transition,
    Wave,
    WaveCoreState,
)

CORE_RULE_VERSION = "malf-core-v1.4"
CORE_EVENT_ORDERING_VERSION = "core-event-order-v1"
PRICE_COMPARE_POLICY = "strict"


@dataclass
class CoreRunResult:
    snapshots: list[CoreStateSnapshot]
    pivots: list[Pivot]
    waves: list[Wave]
    breaks: list[Break]
    transitions: list[Transition]


class CoreEngine:
    """单标的单 timeframe 的 Core 状态机。"""

    def __init__(
        self,
        symbol: str,
        *,
        timeframe: str = "day",
        k: int = 2,
        source_run_id: str = "adhoc",
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.k = k
        self.source_run_id = source_run_id
        self.pivot_rule_version = rule_version(k)

        self.system_state = SystemState.UNINITIALIZED
        self.active_wave: Wave | None = None
        self.transition: Transition | None = None

        self._pivots: list[Pivot] = []
        self._pivots_by_id: dict[int, Pivot] = {}
        self._waves: list[Wave] = []
        self._breaks: list[Break] = []
        self._transitions: list[Transition] = []
        self._snapshots: list[CoreStateSnapshot] = []

        self._init_seq: list[Pivot] = []  # 初始化阶段 reduced 交替序列
        self._next_pivot_id = 1
        self._next_wave_id = 1
        self._next_transition_id = 1

    # --- 主循环 -----------------------------------------------------------
    def run(self, bars: list[DailyBar]) -> CoreRunResult:
        raw = detect_pivots_incremental(bars, self.k)
        by_confirm: dict[date, list[RawPivot]] = defaultdict(list)
        for rp in raw:
            by_confirm[rp.confirm_bar_dt].append(rp)

        for bar in bars:
            # 2-3 + 6-8: 处理本 bar 确认的 pivot（按 extreme_index/kind 稳定排序）
            for rp in by_confirm.get(bar.bar_dt, []):
                piv = self._register_pivot(rp)
                self._on_pivot(piv, bar)
            # 4-5: 逐 bar break 评估
            self._evaluate_break(bar)
            # 9: snapshot
            self._snapshots.append(self._make_snapshot(bar))

        return CoreRunResult(
            snapshots=self._snapshots,
            pivots=self._pivots,
            waves=self._waves,
            breaks=self._breaks,
            transitions=self._transitions,
        )

    # --- pivot 注册 -------------------------------------------------------
    def _register_pivot(self, rp: RawPivot) -> Pivot:
        piv = Pivot(
            pivot_id=self._next_pivot_id,
            kind=rp.kind,
            extreme_bar_dt=rp.extreme_bar_dt,
            confirm_bar_dt=rp.confirm_bar_dt,
            price=rp.price,
            pivot_seq_in_bar=0,
        )
        self._next_pivot_id += 1
        self._pivots.append(piv)
        self._pivots_by_id[piv.pivot_id] = piv
        return piv

    def _on_pivot(self, piv: Pivot, bar: DailyBar) -> None:
        if self.system_state == SystemState.UNINITIALIZED:
            self._handle_init(piv)
        elif self.system_state in (SystemState.UP_ALIVE, SystemState.DOWN_ALIVE):
            self._handle_active_pivot(piv)
        elif self.system_state == SystemState.TRANSITION:
            self._handle_transition_pivot(piv, bar)

    # --- 初始化（D18 / O6）------------------------------------------------
    def _handle_init(self, piv: Pivot) -> None:
        seq = self._init_seq
        if seq and seq[-1].kind == piv.kind:
            # 同类相邻：保留更极端者（O6：更高 H 替换 H0 / 更低 L 替换 L0）
            last = seq[-1]
            if piv.kind == PivotKind.HIGH and piv.price > last.price:
                seq[-1] = piv
            elif piv.kind == PivotKind.LOW and piv.price < last.price:
                seq[-1] = piv
            # 否则丢弃（非更极端，不构成新参考）
        else:
            seq.append(piv)

        if len(seq) < 3:
            return
        a, b, c = seq[-3], seq[-2], seq[-1]
        # H0 -> L1 -> H2 且 H2 > H0：initial up
        if (
            a.kind == PivotKind.HIGH
            and b.kind == PivotKind.LOW
            and c.kind == PivotKind.HIGH
            and c.price > a.price
        ):
            self._create_initial_wave(Direction.UP, start=a, guard=b, progress=c)
        # L0 -> H1 -> L2 且 L2 < L0：initial down
        elif (
            a.kind == PivotKind.LOW
            and b.kind == PivotKind.HIGH
            and c.kind == PivotKind.LOW
            and c.price < a.price
        ):
            self._create_initial_wave(Direction.DOWN, start=a, guard=b, progress=c)

    def _create_initial_wave(
        self, direction: Direction, *, start: Pivot, guard: Pivot, progress: Pivot
    ) -> None:
        wave = Wave(
            wave_id=self._next_wave_id,
            direction=direction,
            start_bar_dt=start.extreme_bar_dt,
            start_pivot_id=start.pivot_id,
            wave_core_state=WaveCoreState.ALIVE,
            current_guard_pivot_id=guard.pivot_id,
            current_guard_price=guard.price,
            progress_extreme_pivot_id=progress.pivot_id,
            progress_extreme_price=progress.price,
        )
        self._next_wave_id += 1
        if direction == Direction.UP:
            guard.primitive = Primitive.HL
            progress.primitive = Primitive.HH
            self.system_state = SystemState.UP_ALIVE
        else:
            guard.primitive = Primitive.LH
            progress.primitive = Primitive.LL
            self.system_state = SystemState.DOWN_ALIVE
        self._waves.append(wave)
        self.active_wave = wave
        self._init_seq = []

    # --- active wave pivot（D9 guard 唯一性 / D4 primitive）---------------
    def _handle_active_pivot(self, piv: Pivot) -> None:
        w = self.active_wave
        assert w is not None
        if w.direction == Direction.UP:
            if piv.kind == PivotKind.HIGH:
                if piv.price > (w.progress_extreme_price or float("-inf")):
                    piv.primitive = Primitive.HH
                    w.progress_extreme_pivot_id = piv.pivot_id
                    w.progress_extreme_price = piv.price
                else:
                    piv.primitive = Primitive.LH
            else:  # L
                if piv.price > (w.current_guard_price or float("-inf")):
                    piv.primitive = Primitive.HL
                    w.current_guard_pivot_id = piv.pivot_id
                    w.current_guard_price = piv.price
                else:
                    piv.primitive = Primitive.LL  # break 应已先触发（异常留痕）
        else:  # DOWN
            if piv.kind == PivotKind.LOW:
                if piv.price < (w.progress_extreme_price or float("inf")):
                    piv.primitive = Primitive.LL
                    w.progress_extreme_pivot_id = piv.pivot_id
                    w.progress_extreme_price = piv.price
                else:
                    piv.primitive = Primitive.HL
            else:  # H
                if piv.price < (w.current_guard_price or float("inf")):
                    piv.primitive = Primitive.LH
                    w.current_guard_pivot_id = piv.pivot_id
                    w.current_guard_price = piv.price
                else:
                    piv.primitive = Primitive.HH  # break 应已先触发

    # --- break（D10 / O3 严格）-------------------------------------------
    def _evaluate_break(self, bar: DailyBar) -> None:
        w = self.active_wave
        if w is None:
            return
        if w.direction == Direction.UP:
            if normalize_price(bar.low) < (w.current_guard_price or float("-inf")):
                self._do_break(bar, w, broken_price=normalize_price(bar.low))
        else:
            if normalize_price(bar.high) > (w.current_guard_price or float("inf")):
                self._do_break(bar, w, broken_price=normalize_price(bar.high))

    def _do_break(self, bar: DailyBar, w: Wave, *, broken_price: float) -> None:
        w.wave_core_state = WaveCoreState.TERMINATED
        w.end_bar_dt = bar.bar_dt
        brk = Break(
            old_wave_id=w.wave_id,
            old_direction=w.direction,
            broken_guard_pivot_id=w.current_guard_pivot_id,
            broken_guard_price=w.current_guard_price,
            break_bar_dt=bar.bar_dt,
            break_price=broken_price,
        )
        self._breaks.append(brk)
        # D13 双边界
        if w.direction == Direction.UP:
            boundary_high = w.progress_extreme_price  # old final HH
            boundary_low = w.current_guard_price       # broken HL
        else:
            boundary_high = w.current_guard_price       # broken LH
            boundary_low = w.progress_extreme_price      # old final LL
        trans = Transition(
            transition_id=self._next_transition_id,
            old_wave_id=w.wave_id,
            old_direction=w.direction,
            open_bar_dt=bar.bar_dt,
            boundary_high=boundary_high if boundary_high is not None else broken_price,
            boundary_low=boundary_low if boundary_low is not None else broken_price,
        )
        self._next_transition_id += 1
        self._transitions.append(trans)
        self.transition = trans
        self.active_wave = None
        self.system_state = SystemState.TRANSITION

    # --- transition pivot（D14/D15/D16 + O4/O5 + T5/T6）------------------
    def _handle_transition_pivot(self, piv: Pivot, bar: DailyBar) -> None:
        t = self.transition
        assert t is not None
        # 先判：piv 是否确认现有 active candidate（D16 after，O2 step7）
        if (
            t.active_candidate_direction == Direction.UP
            and piv.kind == PivotKind.HIGH
            and normalize_price(piv.price) > t.boundary_high
        ):
            self._confirm_new_wave(Direction.UP, guard_id=t.active_candidate_pivot_id, progress=piv, bar=bar)
            return
        if (
            t.active_candidate_direction == Direction.DOWN
            and piv.kind == PivotKind.LOW
            and normalize_price(piv.price) < t.boundary_low
        ):
            self._confirm_new_wave(Direction.DOWN, guard_id=t.active_candidate_pivot_id, progress=piv, bar=bar)
            return
        # 否则 piv 成为新的 active candidate（O4 latest 替换；T5 flip-flop）
        new_dir = Direction.UP if piv.kind == PivotKind.LOW else Direction.DOWN
        if t.active_candidate_pivot_id is not None:
            t.candidate_replacement_count += 1
        t.active_candidate_pivot_id = piv.pivot_id
        t.active_candidate_direction = new_dir

    def _confirm_new_wave(
        self, direction: Direction, *, guard_id: int | None, progress: Pivot, bar: DailyBar
    ) -> None:
        t = self.transition
        assert t is not None and guard_id is not None
        guard = self._pivots_by_id[guard_id]
        wave = Wave(
            wave_id=self._next_wave_id,
            direction=direction,
            start_bar_dt=guard.extreme_bar_dt,
            start_pivot_id=guard.pivot_id,
            wave_core_state=WaveCoreState.ALIVE,
            current_guard_pivot_id=guard.pivot_id,
            current_guard_price=guard.price,
            progress_extreme_pivot_id=progress.pivot_id,
            progress_extreme_price=progress.price,
        )
        self._next_wave_id += 1
        if direction == Direction.UP:
            guard.primitive = Primitive.HL
            progress.primitive = Primitive.HH
            self.system_state = SystemState.UP_ALIVE
        else:
            guard.primitive = Primitive.LH
            progress.primitive = Primitive.LL
            self.system_state = SystemState.DOWN_ALIVE
        self._waves.append(wave)
        self.active_wave = wave
        t.resolved_bar_dt = bar.bar_dt
        t.new_wave_id = wave.wave_id
        self.transition = None

    # --- snapshot（O7）---------------------------------------------------
    def _make_snapshot(self, bar: DailyBar) -> CoreStateSnapshot:
        w = self.active_wave
        t = self.transition
        last_break = self._breaks[-1] if self._breaks and self._breaks[-1].break_bar_dt == bar.bar_dt else None
        new_confirmed = bool(t is None and last_break is None and w is not None and w.start_bar_dt == bar.bar_dt)
        # new wave 确认本 bar：transition 刚 resolved
        resolved_now = any(tr.resolved_bar_dt == bar.bar_dt for tr in self._transitions)
        snap = CoreStateSnapshot(
            symbol=self.symbol,
            timeframe=self.timeframe,
            bar_dt=bar.bar_dt,
            system_state=self.system_state,
            active_wave_id=w.wave_id if w else None,
            old_wave_id=t.old_wave_id if t else None,
            direction=(w.direction if w else (t.old_direction if t else None)),
            wave_core_state=(w.wave_core_state if w else None),
            current_effective_guard_pivot_id=(w.current_guard_pivot_id if w else None),
            current_effective_guard_price=(w.current_guard_price if w else None),
            progress_extreme_pivot_id=(w.progress_extreme_pivot_id if w else None),
            progress_extreme_price=(w.progress_extreme_price if w else None),
            open_transition_id=(t.transition_id if t else None),
            active_candidate_guard_pivot_id=(t.active_candidate_pivot_id if t else None),
            active_candidate_direction=(t.active_candidate_direction if t else None),
            transition_boundary_high=(t.boundary_high if t else None),
            transition_boundary_low=(t.boundary_low if t else None),
            break_event=last_break,
            new_wave_confirmed=resolved_now,
        )
        return snap
