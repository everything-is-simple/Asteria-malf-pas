"""结构目标计算（T1/T2/RR 锚），纯函数无 I/O（D2/D3/D5）。

放在 signal 层（而非 backtest），让 backtest/rules.open_position 从此处导入——
保持 signal → backtest 单向依赖合法，绝不让 signal 依赖 backtest。

输入只有裸价（entry/stop + MALF 结构价 progress_extreme/guard），不依赖任何上层契约。

锚定逻辑（做多/上升波）：
  risk_unit = entry − stop                                   1R
  one_r     = entry + target_r × risk_unit                  1R 基准目标
  has_struct = progress_extreme 存在且 > entry               有可测上行空间
  rr_target = progress_extreme（有结构）/ one_r（无结构）      D3：RR 对结构前高算
  target1   = min(progress_extreme, one_r)（有结构）/ one_r   D5：取近者
  target2   = progress_extreme + (progress_extreme − guard)   D2：量度移动投影
              仅当 has_struct 且 guard 存在且 progress_extreme>guard 且 投影>target1

边界：
  - 无 progress_extreme / ≤ entry → has_struct=False → rr_target=one_r → RR=1.0
    （< min_reward_risk=1.5 → 上游拒绝；只做有可测空间的干净上升波）。
  - guard 缺失 / ≥ progress_extreme → target2=None → 第二部分纯靠跟踪止损兜底。
  - 投影 target2 ≤ target1 → 丢弃为 None。
"""

from __future__ import annotations

from dataclasses import dataclass

_PRICE_EPS = 1e-9


def _round2(x: float) -> float:
    return round(x, 2)


@dataclass(frozen=True)
class StructuralTargets:
    """结构目标计算结果。"""

    target1: float
    target2: float | None
    rr_target: float  # RR 门用的目标参考（结构前高 / 无结构时退 1R 基准）
    risk_unit: float  # 1R（应用 min_risk 地板后的有效风险距离）
    effective_stop: float  # 应用 min_risk 后的止损（正常笔 ≤ 原 stop；退化笔 = 原 stop）


def compute_structural_targets(
    *,
    entry: float,
    stop: float,
    progress_extreme: float | None,
    guard: float | None,
    target_r: float = 1.0,
    min_risk_pct: float = 0.0,
) -> StructuralTargets:
    """算结构 T1/T2 + RR 锚（纯函数，确定性）。

    entry/stop：进场价与初始止损（做多 entry > stop）。
    progress_extreme：MALF up-wave 已创最高 HH（前高/最近阻力）。
    guard：MALF 最近确认 HL（保护性 swing low）。
    target_r：1R 基准目标倍数（默认 1.0）。
    min_risk_pct：最小风险距离占 entry 比例（修复 RR 虚高）。0=不启用（向后兼容）。
        T0 收盘贴近最低时 raw 1R 趋近 stop_offset，RR 被极小分母架空到几十；
        floor 到 min_risk_pct×entry 让 stop/1R/RR/仓位用同一个诚实风险值。

    退化笔（raw_risk ≤ 0，进场跳空到止损下方）不 floor：保留原 stop 与 ≤0 的
    risk_unit，交由调用方守卫（rules.advance）按止损退出，不被 min_risk 误救活。
    """
    raw_risk = entry - stop
    if raw_risk <= 0:
        # 退化：进场已在止损下方 → 不 floor，原样返回（调用方守卫处理）
        effective_risk = raw_risk
        effective_stop = _round2(stop)
    else:
        min_risk = min_risk_pct * entry if min_risk_pct > 0 else 0.0
        effective_risk = max(raw_risk, min_risk)
        effective_stop = _round2(entry - effective_risk)

    one_r = _round2(entry + target_r * effective_risk)

    has_struct = progress_extreme is not None and progress_extreme > entry + _PRICE_EPS

    if has_struct:
        assert progress_extreme is not None  # narrow for type-checkers
        rr_target = progress_extreme
        target1 = _round2(min(progress_extreme, one_r))
    else:
        rr_target = one_r
        target1 = one_r

    target2: float | None = None
    if (
        has_struct
        and guard is not None
        and progress_extreme is not None
        and progress_extreme > guard + _PRICE_EPS
    ):
        projection = _round2(progress_extreme + (progress_extreme - guard))
        if projection > target1 + _PRICE_EPS:
            target2 = projection

    return StructuralTargets(
        target1=target1,
        target2=target2,
        rr_target=rr_target,
        risk_unit=effective_risk,
        effective_stop=effective_stop,
    )
