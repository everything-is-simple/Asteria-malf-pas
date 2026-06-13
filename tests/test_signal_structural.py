"""结构目标计算测试（compute_structural_targets，D2/D3/D5）。

覆盖：前高近于 1R / 1R 近于前高 / 无前高 / T2 量度投影 / T2≤T1 丢弃 / guard 缺失。
全手算 oracle，纯函数无 I/O。
"""

from asteria.signal.structural import compute_structural_targets


def test_no_progress_extreme_falls_back_to_1r():
    """无结构前高 → rr_target=one_r，RR 基准=1.0，target2=None。"""
    st = compute_structural_targets(
        entry=10.0, stop=9.48, progress_extreme=None, guard=None, target_r=1.0
    )
    assert abs(st.risk_unit - 0.52) < 1e-9
    assert st.target1 == 10.52        # entry + 1R
    assert st.rr_target == 10.52      # 退 1R 基准
    assert st.target2 is None
    # RR = (10.52-10.00)/0.52 = 1.0
    assert (st.rr_target - 10.0) / st.risk_unit == 1.0


def test_progress_extreme_below_entry_no_struct():
    """前高 ≤ entry（已在前高之上，无头顶阻力）→ 退 1R，无结构空间。"""
    st = compute_structural_targets(
        entry=10.0, stop=9.48, progress_extreme=9.90, guard=9.60, target_r=1.0
    )
    assert st.rr_target == 10.52      # one_r，非 9.90
    assert st.target1 == 10.52
    assert st.target2 is None         # has_struct=False → 无 T2


def test_front_high_far_target1_is_1r():
    """前高远于 1R（>1.5R）→ rr_target=前高、target1=min=entry+1R（D5）。"""
    # entry=10, stop=9.48, 1R=0.52, one_r=10.52；前高=11.06（在 2R 外）
    st = compute_structural_targets(
        entry=10.0, stop=9.48, progress_extreme=11.06, guard=9.60, target_r=1.0
    )
    assert st.rr_target == 11.06      # D3：RR 对结构前高算
    assert st.target1 == 10.52        # D5：min(11.06, 10.52)=10.52
    # RR = (11.06-10.00)/0.52 ≈ 2.04
    assert abs((st.rr_target - 10.0) / st.risk_unit - 2.0385) < 1e-3


def test_front_high_near_target1_is_front_high():
    """前高近于 1R（< one_r）→ target1=min=前高（D5 取近的退化保护）。"""
    # one_r=10.52；前高=10.30（在 1R 内）
    st = compute_structural_targets(
        entry=10.0, stop=9.48, progress_extreme=10.30, guard=9.60, target_r=1.0
    )
    assert st.rr_target == 10.30
    assert st.target1 == 10.30        # min(10.30, 10.52)=10.30
    # RR = (10.30-10.00)/0.52 ≈ 0.577 < 1.5（该拒绝）
    assert (st.rr_target - 10.0) / st.risk_unit < 1.5


def test_target2_measured_move_projection():
    """T2 = 前高 + (前高 − guard)（量度移动投影），> target1 才保留。"""
    # 前高=11.06, guard=9.60 → rng=1.46 → T2=12.52
    st = compute_structural_targets(
        entry=10.0, stop=9.48, progress_extreme=11.06, guard=9.60, target_r=1.0
    )
    assert st.target2 == 12.52
    assert st.target2 > st.target1


def test_target2_discarded_when_not_above_target1():
    """投影 T2 ≤ target1 → 丢弃为 None。"""
    # 前高=10.30(near, target1=10.30), guard=10.20 → rng=0.10 → 投影=10.40 > 10.30 → 保留
    # 构造 ≤ target1：guard 极近前高使投影 ≤ target1 不现实；改用前高略高于 one_r 但投影落在 target1 下方不可能。
    # 用 guard 等于前高的边界（projection 会 == 前高 == 不 > target1 的退化）：
    st = compute_structural_targets(
        entry=10.0, stop=9.48, progress_extreme=10.60, guard=10.60, target_r=1.0
    )
    # guard == 前高 → progress_extreme > guard 不成立 → target2=None
    assert st.target2 is None


def test_guard_none_target2_none():
    """guard 缺失 → T2=None（第二部分纯跟踪兜底）。"""
    st = compute_structural_targets(
        entry=10.0, stop=9.48, progress_extreme=11.06, guard=None, target_r=1.0
    )
    assert st.rr_target == 11.06      # 仍有结构 RR
    assert st.target2 is None         # 但无 guard → 无量度 T2


# =========================================================================
# 修复1：min_risk_pct 最小风险距离地板（修止损微小→RR 虚高）
# =========================================================================
def test_min_risk_floor_widens_tiny_stop():
    """T0 收盘贴近最低 → raw 1R 微小（0.16）；min_risk_pct=0.02 地板到 0.02×entry。

    entry=36.61, stop=36.45 → raw_risk=0.16；floor=0.02×36.61=0.7322。
    effective_risk=max(0.16, 0.7322)=0.7322；effective_stop=36.61-0.7322≈35.88。
    """
    st = compute_structural_targets(
        entry=36.61, stop=36.45, progress_extreme=46.45, guard=9.46,
        target_r=1.0, min_risk_pct=0.02,
    )
    assert abs(st.risk_unit - 0.7322) < 1e-4
    assert st.effective_stop == 35.88
    # RR 由虚高的 61.5 压回到诚实值 (46.45-36.61)/0.7322 ≈ 13.4——仍高但不再被极小分母架空
    rr = (st.rr_target - 36.61) / st.risk_unit
    assert abs(rr - 13.44) < 0.1
    # 对照：不启用 min_risk 时 RR 虚高到 61.5
    st0 = compute_structural_targets(
        entry=36.61, stop=36.45, progress_extreme=46.45, guard=9.46, target_r=1.0
    )
    assert abs((st0.rr_target - 36.61) / st0.risk_unit - 61.5) < 0.5


def test_min_risk_floor_inactive_when_stop_already_wide():
    """raw 1R 已 ≥ 地板 → 不动 stop（floor 只收窄不放宽真实风险）。"""
    # entry=10, stop=9.48 → raw_risk=0.52；floor=0.02×10=0.20 < 0.52 → 不变
    st = compute_structural_targets(
        entry=10.0, stop=9.48, progress_extreme=11.06, guard=9.60,
        target_r=1.0, min_risk_pct=0.02,
    )
    assert abs(st.risk_unit - 0.52) < 1e-9
    assert st.effective_stop == 9.48


def test_min_risk_floor_not_applied_to_degenerate():
    """raw_risk ≤ 0（跳空到止损下方）→ 不 floor，原样返回（交给守卫）。"""
    # entry=6.05, stop=6.07 → raw_risk=-0.02 ≤ 0
    st = compute_structural_targets(
        entry=6.05, stop=6.07, progress_extreme=6.33, guard=6.00,
        target_r=1.0, min_risk_pct=0.02,
    )
    assert st.risk_unit < 0            # 不被 floor 救活（保持负值）
    assert abs(st.risk_unit - (-0.02)) < 1e-9
    assert st.effective_stop == 6.07   # 原 stop
