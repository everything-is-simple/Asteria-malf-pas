"""Page 3: 单标的结构可视化（Structure Inspector）。

K 线 + pivot(H/L) + wave 段(up/down 着色) + current guard 线 + transition 边界带
+ break 标记。这是 M1 验证 MALF 正确性的核心调试视图。
"""

from __future__ import annotations

from datetime import date

import plotly.graph_objects as go
import streamlit as st

from asteria.data import loader
from asteria.malf.runner import run_symbol
from asteria.malf.types import Direction, PivotKind


def _candlestick(bars) -> go.Figure:
    fig = go.Figure(
        data=[
            go.Candlestick(
                x=[b.bar_dt for b in bars],
                open=[b.open for b in bars],
                high=[b.high for b in bars],
                low=[b.low for b in bars],
                close=[b.close for b in bars],
                name="K",
                increasing_line_color="#d62728",
                decreasing_line_color="#2ca02c",
            )
        ]
    )
    fig.update_layout(
        xaxis_rangeslider_visible=False,
        height=640,
        margin=dict(l=8, r=8, t=28, b=8),
        showlegend=True,
    )
    return fig


def _overlay_pivots(fig: go.Figure, result) -> None:
    highs = [p for p in result.pivots if p.kind == PivotKind.HIGH]
    lows = [p for p in result.pivots if p.kind == PivotKind.LOW]
    if highs:
        fig.add_trace(
            go.Scatter(
                x=[p.extreme_bar_dt for p in highs],
                y=[p.price for p in highs],
                mode="markers",
                marker=dict(symbol="triangle-down", size=9, color="#9467bd"),
                name="H pivot",
                text=[p.primitive.value if p.primitive else "" for p in highs],
            )
        )
    if lows:
        fig.add_trace(
            go.Scatter(
                x=[p.extreme_bar_dt for p in lows],
                y=[p.price for p in lows],
                mode="markers",
                marker=dict(symbol="triangle-up", size=9, color="#1f77b4"),
                name="L pivot",
                text=[p.primitive.value if p.primitive else "" for p in lows],
            )
        )


def _overlay_waves(fig: go.Figure, result) -> None:
    """每条 wave 用 start→end 连线着色（up 红 / down 绿）。"""
    for w in result.waves:
        color = "#d62728" if w.direction == Direction.UP else "#2ca02c"
        end_dt = w.end_bar_dt or (result.snapshots[-1].bar_dt if result.snapshots else w.start_bar_dt)
        y0 = w.current_guard_price
        y1 = w.progress_extreme_price
        if y0 is None or y1 is None:
            continue
        fig.add_trace(
            go.Scatter(
                x=[w.start_bar_dt, end_dt],
                y=[y0, y1],
                mode="lines",
                line=dict(color=color, width=1.5, dash="dot"),
                name=f"wave#{w.wave_id} {w.direction.value}",
                showlegend=False,
                opacity=0.6,
            )
        )


def _overlay_breaks(fig: go.Figure, result) -> None:
    if not result.breaks:
        return
    fig.add_trace(
        go.Scatter(
            x=[b.break_bar_dt for b in result.breaks],
            y=[b.break_price for b in result.breaks],
            mode="markers",
            marker=dict(symbol="x", size=10, color="black"),
            name="break",
        )
    )


def render() -> None:
    st.header("结构可视化 · Structure Inspector")

    symbols = loader.list_symbols()
    if not symbols:
        st.warning("market.sqlite 无数据，请先运行 scripts/ingest_data.py 灌数。")
        return

    col1, col2, col3 = st.columns([2, 1, 1])
    symbol = col1.selectbox("标的", symbols)
    k = col2.number_input("pivot_k", min_value=1, max_value=10, value=2, step=1)
    inst = loader.get_instrument(symbol)
    if inst is not None:
        col3.metric("名称", inst["name"] or "-")

    bars_all = loader.load_bars(symbol)
    if not bars_all:
        st.warning(f"{symbol} 无后复权数据。")
        return

    min_dt, max_dt = bars_all[0].bar_dt, bars_all[-1].bar_dt
    dr = st.slider(
        "日期范围",
        min_value=min_dt,
        max_value=max_dt,
        value=(max(min_dt, date(max(min_dt.year, max_dt.year - 1), 1, 1)), max_dt),
    )
    start_dt, end_dt = dr

    bars, result = run_symbol(symbol, k=int(k), start_dt=start_dt, end_dt=end_dt)
    if not bars:
        st.warning("所选范围无数据。")
        return

    fig = _candlestick(bars)
    _overlay_waves(fig, result)
    _overlay_pivots(fig, result)
    _overlay_breaks(fig, result)
    st.plotly_chart(fig, use_container_width=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("pivots", len(result.pivots))
    c2.metric("waves", len(result.waves))
    c3.metric("breaks", len(result.breaks))
    c4.metric("system_state", result.snapshots[-1].system_state.value if result.snapshots else "-")

    with st.expander("最近 wave 明细"):
        st.dataframe(
            [
                {
                    "wave_id": w.wave_id,
                    "direction": w.direction.value,
                    "state": w.wave_core_state.value,
                    "start": w.start_bar_dt.isoformat(),
                    "end": w.end_bar_dt.isoformat() if w.end_bar_dt else "",
                    "guard": w.current_guard_price,
                    "progress": w.progress_extreme_price,
                }
                for w in result.waves[-20:]
            ]
        )


if __name__ == "__main__":
    render()
