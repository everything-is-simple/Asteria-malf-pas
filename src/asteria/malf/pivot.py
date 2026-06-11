"""pivot 检测：分形确认（fractal-k）。

规则（D2 由实现层指定）：一个 bar 的 high 严格高于其前后各 k 根 bar 的 high
→ 确认为 H（L 对称用 low）。确认有延迟：极值 bar 之后第 k 根 bar 才能确认（O2）。
- extreme_bar = 极值所在 bar；confirm_bar = extreme_bar 后第 k 根 bar。
- pivot 价格属于 extreme bar；确认事件时间 = confirm bar 的 dt。
确定性：算法固定 + k 固定 ⇒ pivot 序列唯一。rule_version = 'fractal-k{K}-v1'。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from asteria.data.contracts import DailyBar
from asteria.malf.types import PivotKind

PRICE_DP = 2  # 价格归一化精度（O3：比较前 round 到 2 位）


def normalize_price(px: float) -> float:
    return round(px, PRICE_DP)


def rule_version(k: int) -> str:
    return f"fractal-k{k}-v1"


@dataclass(frozen=True)
class RawPivot:
    """检测出的原始极值（未编号、未判 primitive）。"""

    kind: PivotKind
    extreme_bar_dt: date
    confirm_bar_dt: date
    price: float
    extreme_index: int  # 在序列中的位置（用于排序/去重）


def detect_pivots_incremental(
    bars: list[DailyBar], k: int
) -> list[RawPivot]:
    """对完整 bar 序列做一次性分形检测，返回按 confirm_bar_dt 升序的 pivot。

    用于离线/回放：逐 bar 推进时，core 只消费 confirm_bar_dt <= 当前 bar 的 pivot，
    保证无未来函数（极值确认延迟 k 根天然满足因果）。
    候选极值 i 需满足 k <= i <= len-1-k。
    """
    n = len(bars)
    pivots: list[RawPivot] = []
    if n < 2 * k + 1:
        return pivots
    for i in range(k, n - k):
        hi = normalize_price(bars[i].high)
        lo = normalize_price(bars[i].low)
        window = range(i - k, i + k + 1)
        is_high = all(
            normalize_price(bars[j].high) <= hi for j in window if j != i
        ) and all(
            normalize_price(bars[j].high) < hi for j in (i - k, i + k)
        )
        is_low = all(
            normalize_price(bars[j].low) >= lo for j in window if j != i
        ) and all(
            normalize_price(bars[j].low) > lo for j in (i - k, i + k)
        )
        confirm_dt = bars[i + k].bar_dt
        if is_high:
            pivots.append(
                RawPivot(PivotKind.HIGH, bars[i].bar_dt, confirm_dt, hi, i)
            )
        if is_low:
            pivots.append(
                RawPivot(PivotKind.LOW, bars[i].bar_dt, confirm_dt, lo, i)
            )
    pivots.sort(key=lambda p: (p.confirm_bar_dt, p.extreme_index, p.kind.value))
    return pivots
