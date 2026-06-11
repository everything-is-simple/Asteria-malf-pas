"""Streamlit 多页入口。

M1 只挂 Structure Inspector（单标的结构可视化），后续里程碑再加机会列表/回测结果。

运行：
  streamlit run src/asteria/ui/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让 src 布局可被 import（streamlit 直接跑文件，需手动注入路径）
_REPO = Path(__file__).resolve().parents[3]
for p in (str(_REPO / "src"), str(_REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

import streamlit as st  # noqa: E402

from asteria.ui.pages import symbol_view  # noqa: E402

st.set_page_config(page_title="Asteria-Malf-Pas", layout="wide")

PAGES = {
    "结构可视化 (Structure Inspector)": symbol_view.render,
}

st.sidebar.title("Asteria-Malf-Pas")
choice = st.sidebar.radio("页面", list(PAGES.keys()))
PAGES[choice]()
