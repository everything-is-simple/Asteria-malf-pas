"""MALF-Lifespan：波段生命统计（MALF v1.4，L2–L18）。

建立在 Core 已确认 wave 之上，逐 bar 把 wave/break/transition 事件翻译成
「生命统计位置」WavePosition。本层**不**确认 wave 是否成立（那是 Core 的职责），
只描述当前活波推进了几次、停滞多久、在同类样本中的相对位置、如何出生。

事件对齐（与 core.py 的 O2 九步一致）：逐 bar 单趟遍历 CoreRunResult.snapshots，
维护「当前 wave 上下文」（wave_id / 上次推进 bar 序号 / new_count 累加器）。
wave 切换（break → transition → new wave）时重置或冻结上下文。

🔒 Lifespan 铁律：
- rank 是历史位置不是概率（L-T6）。
- birth 描述形成过程不描述未来收益（L-T7）。
- system_state=transition 时 direction = old_direction（L-T5）。

MVP 简化（接口保留完整）：
- rank 用本 run 自洽经验分布（同 timeframe + 同 direction 的已 terminated wave 终值），
  sample_cutoff ≤ 当前 bar_dt 防前视。预留 peer_provider 注入外部样本。
- candidate_wait_span：Core 未记录 candidate 首次出现 bar，本层用 transition
  open→resolved 跨度近似（上界），并在 reason 留痕。不改 Core（避免 M1 返工）。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date

from asteria.data.contracts import DailyBar
from asteria.malf.core import CoreRunResult
from asteria.malf.pivot import normalize_price
from asteria.malf.types import (
    BirthType,
    CoreStateSnapshot,
    Direction,
    LifeState,
    PositionQuadrant,
    SystemState,
    Transition,
    Wave,
    WaveCoreState,
    WavePosition,
)

LIFESPAN_RULE_VERSION = "malf-lifespan-v1.4-mvp"
SAMPLE_VERSION = "peer-empirical-v1"


@dataclass(frozen=True)
class LifespanConfig:
    """Lifespan 阈值与版本（默认值同 config/params_default.toml 的 [malf.lifespan]）。

    用 dataclass 承载便于测试注入；不读 toml，避免引入配置加载依赖。
    """

    high_stagnation_threshold: float = 0.8
    high_update_threshold: float = 0.8
    low_update_threshold: float = 0.2
    sample_version: str = SAMPLE_VERSION
    lifespan_rule_version: str = LIFESPAN_RULE_VERSION


# 顶层默认配置（不可变，安全共享）
DEFAULT_CONFIG = LifespanConfig()


# =========================================================================
# 内部：wave 终值样本（用于 rank 经验分布）
# =========================================================================
@dataclass
class _WaveFinalStat:
    """一条已 terminated wave 的统计终值（入 peer 样本）。"""

    direction: Direction
    end_bar_dt: date
    final_new_count: int
    final_no_new_span: int


@dataclass
class _PeerSample:
    """本 run 自洽经验分布：同方向已完成 wave 的终值，按 end_bar_dt 升序便于 cutoff。

    cutoff 语义：只用 end_bar_dt ≤ 当前 bar_dt 的样本算 rank，防前视（L9）。
    """

    by_direction: dict[Direction, list[_WaveFinalStat]] = field(default_factory=dict)

    def add(self, stat: _WaveFinalStat) -> None:
        self.by_direction.setdefault(stat.direction, []).append(stat)

    def finalize(self) -> None:
        for stats in self.by_direction.values():
            stats.sort(key=lambda s: s.end_bar_dt)

    def _eligible(self, direction: Direction, cutoff: date) -> tuple[list[int], list[int]]:
        """返回 (new_counts, no_new_spans)：end_bar_dt ≤ cutoff 的同向样本终值。"""
        stats = self.by_direction.get(direction, [])
        nc: list[int] = []
        ns: list[int] = []
        for s in stats:
            if s.end_bar_dt <= cutoff:
                nc.append(s.final_new_count)
                ns.append(s.final_no_new_span)
            # stats 已按 end_bar_dt 升序，可提前 break
            else:
                break
        return nc, ns

    def update_rank(self, direction: Direction, value: int, cutoff: date) -> float | None:
        nc, _ = self._eligible(direction, cutoff)
        return _percentile_rank(nc, value)

    def stagnation_rank(self, direction: Direction, value: int, cutoff: date) -> float | None:
        _, ns = self._eligible(direction, cutoff)
        return _percentile_rank(ns, value)


def _percentile_rank(sample: list[int], value: int) -> float | None:
    """标准百分位：样本中 ≤ value 的占比。空样本返回 None。

    单调性保证：value 越大，≤ value 的计数不减 ⇒ rank 不减（验收点）。
    """
    if not sample:
        return None
    ordered = sorted(sample)
    le_count = bisect_right(ordered, value)
    return le_count / len(ordered)


# =========================================================================
# 主入口
# =========================================================================
def compute_wave_positions(
    result: CoreRunResult,
    bars: list[DailyBar],
    *,
    symbol: str,
    timeframe: str = "day",
    source_run_id: str = "adhoc",
    cfg: LifespanConfig = DEFAULT_CONFIG,
) -> list[WavePosition]:
    """逐 bar 产出 WavePosition。

    两趟：
      1. 单趟遍历 snapshots，算每 bar 的结构链路 + 计数（new_count/no_new_span/
         transition_span）+ birth descriptors，并在 wave terminated 时收集 peer 终值。
      2. 用完整 peer 样本（cutoff 控制前视）回填 rank / life_state / quadrant。
    """
    snaps = result.snapshots
    waves_by_id = {w.wave_id: w for w in result.waves}
    transitions_by_id = {t.transition_id: t for t in result.transitions}
    # break/transition 按 old_wave_id 索引，用于 birth_type 判定
    transitions_by_new_wave = {
        t.new_wave_id: t for t in result.transitions if t.new_wave_id is not None
    }

    # ---- 第 1 趟：计数 + 结构链路 + birth + 收集样本 ----
    positions = _pass1_counts(
        snaps,
        bars,
        waves_by_id=waves_by_id,
        transitions_by_id=transitions_by_id,
        transitions_by_new_wave=transitions_by_new_wave,
        symbol=symbol,
        timeframe=timeframe,
        source_run_id=source_run_id,
        cfg=cfg,
    )

    # ---- 第 2 趟：rank + life_state + quadrant ----
    sample = _build_peer_sample(positions, waves_by_id)
    _pass2_rank(positions, sample, cfg=cfg)

    return positions


# =========================================================================
# 第 1 趟：计数 / 结构链路 / birth descriptors
# =========================================================================
def _pass1_counts(
    snaps: list[CoreStateSnapshot],
    bars: list[DailyBar],
    *,
    waves_by_id: dict[int, Wave],
    transitions_by_id: dict[int, Transition],
    transitions_by_new_wave: dict[int, Transition],
    symbol: str,
    timeframe: str,
    source_run_id: str,
    cfg: LifespanConfig,
) -> list[WavePosition]:
    positions: list[WavePosition] = []

    # 当前 wave 上下文
    cur_wave_id: int | None = None
    new_count = 0
    no_new_span = 0
    last_progress_pivot_id: int | None = None  # 检测 progress_extreme 是否被替换
    # transition 上下文
    transition_span = 0  # 本次 transition 已历经 bar 数
    # birth 缓存（P1）：每条 wave 在确认 bar 算一次，后续 bar 复制同一份不再重算，
    # 避免后续 HH/LL 把出生距离改写成后续推进距离（违反 L-T7）。
    birth_by_wave: dict[int, _Birth] = {}

    for snap in snaps:
        ss = snap.system_state

        if ss == SystemState.UNINITIALIZED:
            # 结构未成立：全部计数 0，无 wave 链路
            cur_wave_id = None
            new_count = 0
            no_new_span = 0
            last_progress_pivot_id = None
            transition_span = 0
            positions.append(
                _make_position(
                    snap,
                    symbol=symbol,
                    timeframe=timeframe,
                    source_run_id=source_run_id,
                    cfg=cfg,
                    new_count=0,
                    no_new_span=0,
                    transition_span=0,
                )
            )
            continue

        if ss == SystemState.TRANSITION:
            # break→新波确认之间：transition_span 累加，不并入新波 no_new_span（L5/L-T3）
            transition_span += 1
            # transition 期保留 old_direction（L-T5）由 snapshot.direction 已带
            pos = _make_position(
                snap,
                symbol=symbol,
                timeframe=timeframe,
                source_run_id=source_run_id,
                cfg=cfg,
                new_count=new_count,        # 冻结旧波计数（transition 不属任何 wave，仅承载旧波终值）
                no_new_span=no_new_span,    # 冻结（terminated 已冻结）
                transition_span=transition_span,
                frozen=True,
            )
            # P3：break bar（首个 transition bar，带 break_event）= 旧波终止事件 bar。
            # Core 此刻 active_wave 已置 None，snap.wave_core_state 为 None，
            # 若不在此标记 terminated，compute_wave_positions 将永不产出 life_state=terminal，
            # 使 PAS 的 life_state=terminal 条件成为死分支。这里据 break_event 显式标记，
            # 让 pass2 在真实 Core 输出路径上产出 terminal（direction 已是 old_direction）。
            if snap.break_event is not None:
                pos.wave_core_state = WaveCoreState.TERMINATED
                if pos.old_wave_id is None:
                    pos.old_wave_id = snap.break_event.old_wave_id
                if pos.direction is None:
                    pos.direction = snap.break_event.old_direction
            positions.append(pos)
            continue

        # ---- up_alive / down_alive ----
        wave = waves_by_id.get(snap.active_wave_id) if snap.active_wave_id else None

        if snap.active_wave_id != cur_wave_id:
            # 进入新 wave（initial 或 new wave 确认 bar）：重置计数
            cur_wave_id = snap.active_wave_id
            # 新波确认 bar：初始 progress 记 1 次更新（L3），no_new_span=0
            new_count = 1
            no_new_span = 0
            last_progress_pivot_id = snap.progress_extreme_pivot_id
            transition_span = 0  # 新波开始，transition 上下文清零
            pos = _make_position(
                snap,
                symbol=symbol,
                timeframe=timeframe,
                source_run_id=source_run_id,
                cfg=cfg,
                new_count=new_count,
                no_new_span=no_new_span,
                transition_span=0,
            )
            # birth 在确认 bar 算一次并缓存（P1），后续 bar 复制同一份
            birth = _compute_birth(snap, transitions_by_new_wave, bars)
            birth_by_wave[snap.active_wave_id] = birth
            _apply_birth(pos, birth)
            positions.append(pos)
            continue

        # 同一 wave 持续：判断本 bar 是否发生 progress 推进
        if snap.progress_extreme_pivot_id != last_progress_pivot_id:
            # progress_extreme 被替换 = 一次推进 update（HH/LL）
            new_count += 1
            no_new_span = 0
            last_progress_pivot_id = snap.progress_extreme_pivot_id
        else:
            if snap.wave_core_state == WaveCoreState.TERMINATED:
                # terminated 冻结（不再 +1）
                pass
            else:
                no_new_span += 1

        pos = _make_position(
            snap,
            symbol=symbol,
            timeframe=timeframe,
            source_run_id=source_run_id,
            cfg=cfg,
            new_count=new_count,
            no_new_span=no_new_span,
            transition_span=0,
        )
        # 同一 wave 后续 bar 复制确认 bar 缓存的 birth（P1：恒定，不重算）
        birth = birth_by_wave.get(snap.active_wave_id)
        if birth is not None:
            _apply_birth(pos, birth)
        positions.append(pos)

    return positions


def _make_position(
    snap: CoreStateSnapshot,
    *,
    symbol: str,
    timeframe: str,
    source_run_id: str,
    cfg: LifespanConfig,
    new_count: int,
    no_new_span: int,
    transition_span: int,
    frozen: bool = False,
) -> WavePosition:
    """从 snapshot + 计数构造 WavePosition（rank/life_state 留待第 2 趟回填）。"""
    return WavePosition(
        symbol=symbol,
        timeframe=timeframe,
        bar_dt=snap.bar_dt,
        wave_id=snap.active_wave_id,
        old_wave_id=snap.old_wave_id,
        system_state=snap.system_state,
        wave_core_state=snap.wave_core_state,
        direction=snap.direction,  # transition 时已是 old_direction（L-T5）
        new_count=new_count,
        no_new_span=no_new_span,
        transition_span=transition_span,
        candidate_replacement_count=None,  # birth 填充时覆盖
        sample_version=cfg.sample_version,
        lifespan_rule_version=cfg.lifespan_rule_version,
        source_run_id=source_run_id,
    )


@dataclass(frozen=True)
class _Birth:
    """一条 wave 的出生描述（L13–L17）。**在确认 bar 计算一次**后对整条 wave 恒定。

    🔒 P1 修复：confirmation_distance 必须用「确认 bar」的 progress_extreme（即突破
    boundary 的那个 confirmation pivot）计算。若每根 bar 重算，后续 HH/LL 会把出生
    距离改写成后续推进距离，违反 L-T7「birth 描述形成过程不描述未来收益」。
    """

    birth_type: BirthType | None = None
    candidate_wait_span: int | None = None
    candidate_replacement_count: int | None = None
    confirmation_distance_abs: float | None = None
    confirmation_distance_pct: float | None = None


def _compute_birth(
    snap: CoreStateSnapshot,
    transitions_by_new_wave: dict[int, Transition],
    bars: list[DailyBar],
) -> _Birth:
    """在新波确认 bar 计算 birth descriptors（L13–L17），返回不可变快照。"""
    wave_id = snap.active_wave_id
    if wave_id is None:
        return _Birth()
    trans = transitions_by_new_wave.get(wave_id)
    if trans is None:
        # 没有产生它的 transition → initial wave（首个波）
        return _Birth(birth_type=BirthType.INITIAL)

    # 由 transition 确认：同向 vs 反向（L13）
    if snap.direction == trans.old_direction:
        birth_type = BirthType.SAME_DIRECTION_AFTER_BREAK
    else:
        birth_type = BirthType.OPPOSITE_DIRECTION_AFTER_BREAK

    # candidate_wait_span（L15）：Core 未记录 candidate 首次出现 bar，
    # MVP 用 transition open→resolved 跨度近似（上界）。
    if trans.resolved_bar_dt is not None:
        wait_span = _span_bars(bars, trans.open_bar_dt, trans.resolved_bar_dt)
    else:
        wait_span = None

    # confirmation_distance_abs/pct（L17）：confirmation pivot 价 vs 突破的 boundary。
    # confirmation = 新波在「确认 bar」的 progress_extreme（突破 boundary 的那个 pivot）。
    dist_abs: float | None = None
    dist_pct: float | None = None
    conf_price = snap.progress_extreme_price
    if conf_price is not None and snap.direction is not None:
        boundary = trans.boundary_high if snap.direction == Direction.UP else trans.boundary_low
        if boundary is not None:
            dist = abs(normalize_price(conf_price) - normalize_price(boundary))
            dist_abs = round(dist, 2)
            dist_pct = round(dist / abs(boundary), 6) if boundary != 0 else None

    return _Birth(
        birth_type=birth_type,
        candidate_wait_span=wait_span,
        candidate_replacement_count=trans.candidate_replacement_count,  # L16，Core 已填充
        confirmation_distance_abs=dist_abs,
        confirmation_distance_pct=dist_pct,
    )


def _apply_birth(pos: WavePosition, birth: _Birth) -> None:
    """把缓存的 birth 快照复制到 position（整条 wave 复用同一份，确保不被后续 bar 改写）。"""
    pos.birth_type = birth.birth_type
    pos.candidate_wait_span = birth.candidate_wait_span
    pos.candidate_replacement_count = birth.candidate_replacement_count
    pos.confirmation_distance_abs = birth.confirmation_distance_abs
    pos.confirmation_distance_pct = birth.confirmation_distance_pct


def _span_bars(bars: list[DailyBar], start_dt: date, end_dt: date) -> int:
    """两个 bar_dt 之间的 bar 数（交易日跨度，含端点差）。"""
    dates = [b.bar_dt for b in bars]
    try:
        i0 = dates.index(start_dt)
        i1 = dates.index(end_dt)
    except ValueError:
        return 0
    return max(0, i1 - i0)


# =========================================================================
# 第 2 趟：rank / life_state / quadrant
# =========================================================================
def _build_peer_sample(
    positions: list[WavePosition],
    waves_by_id: dict[int, Wave],
) -> _PeerSample:
    """从已 terminated wave 的终值构造经验分布。

    终值 = 该 wave 在 terminated bar 上的 new_count / no_new_span（冻结值）。
    用 positions 里 wave_core_state=terminated 的最后一条作为终值。
    """
    sample = _PeerSample()
    # 每个 wave_id 记录其 terminated 终值（取该 wave 最后一条 alive/terminated 计数）
    final_by_wave: dict[int, WavePosition] = {}
    for pos in positions:
        if pos.wave_id is None:
            continue
        # 记录该 wave 的最新一条带计数的 position（持续覆盖 → 最后一条即终值）
        if pos.wave_core_state in (WaveCoreState.ALIVE, WaveCoreState.TERMINATED):
            final_by_wave[pos.wave_id] = pos

    for wave_id, pos in final_by_wave.items():
        wave = waves_by_id.get(wave_id)
        if wave is None or wave.wave_core_state != WaveCoreState.TERMINATED:
            continue  # 只有已完成 wave 入样本（防当前 alive wave 自我污染）
        if wave.end_bar_dt is None or pos.direction is None:
            continue
        sample.add(
            _WaveFinalStat(
                direction=pos.direction,
                end_bar_dt=wave.end_bar_dt,
                final_new_count=pos.new_count,
                final_no_new_span=pos.no_new_span,
            )
        )
    sample.finalize()
    return sample


def _pass2_rank(
    positions: list[WavePosition],
    sample: _PeerSample,
    *,
    cfg: LifespanConfig,
) -> None:
    """回填 update_rank / stagnation_rank / life_state / position_quadrant。"""
    for pos in positions:
        # P3：break bar 被标记 terminated（仍是 transition 态），应放行以产出 life_state=terminal。
        is_terminated_break = (
            pos.system_state == SystemState.TRANSITION
            and pos.wave_core_state == WaveCoreState.TERMINATED
        )
        if not is_terminated_break and (
            pos.direction is None
            or pos.system_state in (SystemState.UNINITIALIZED, SystemState.TRANSITION)
        ):
            # 无活波坐标（普通过渡 bar / 未初始化）→ rank/state 留 None（字段级缺失合法）
            continue

        cutoff = pos.bar_dt
        pos.update_rank = sample.update_rank(pos.direction, pos.new_count, cutoff)
        pos.stagnation_rank = sample.stagnation_rank(pos.direction, pos.no_new_span, cutoff)
        pos.life_state = _life_state(pos, cfg)
        pos.position_quadrant = _quadrant(pos, cfg)


def _life_state(pos: WavePosition, cfg: LifespanConfig) -> LifeState:
    """life_state 判定（设计 §3.3，顺序固定）。"""
    if pos.wave_core_state == WaveCoreState.TERMINATED:
        return LifeState.TERMINAL
    sr = pos.stagnation_rank
    ur = pos.update_rank
    if sr is not None and sr >= cfg.high_stagnation_threshold:
        return LifeState.STAGNANT
    if ur is not None and ur >= cfg.high_update_threshold:
        return LifeState.EXTENDED
    if ur is not None and ur < cfg.low_update_threshold:
        return LifeState.EARLY
    return LifeState.DEVELOPING


def _quadrant(pos: WavePosition, cfg: LifespanConfig) -> PositionQuadrant:
    """position_quadrant（设计 §3.4）：update_rank × stagnation_rank 高/低组合。

    中间区（rank 为 None 或落在阈值之间）归 developing。
    """
    ur = pos.update_rank
    sr = pos.stagnation_rank
    if ur is None or sr is None:
        return PositionQuadrant.DEVELOPING
    high_update = ur >= cfg.high_update_threshold
    low_update = ur < cfg.low_update_threshold
    high_stag = sr >= cfg.high_stagnation_threshold

    if low_update and not high_stag:
        return PositionQuadrant.EARLY_ACTIVE
    if low_update and high_stag:
        return PositionQuadrant.EARLY_STAGNANT
    if high_update and not high_stag:
        return PositionQuadrant.EXTENDED_ACTIVE
    if high_update and high_stag:
        return PositionQuadrant.EXTENDED_STAGNANT
    return PositionQuadrant.DEVELOPING
