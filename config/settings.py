"""全局配置：路径、外置目录布局、时间分组、复权、universe 常量。

目录布局（与上一版治理一致）：代码仓库与数据/备份/报告/临时/validated 为同级兄弟目录，
数据文件（*.sqlite）严禁进入仓库根目录。
"""

from __future__ import annotations

from pathlib import Path

# --- 仓库与外置兄弟目录 ---------------------------------------------------
# REPO_ROOT = G:\Asteria-malf-pas\  (本文件位于 REPO_ROOT/config/settings.py)
REPO_ROOT = Path(__file__).resolve().parent.parent
_SIBLING_BASE = REPO_ROOT.parent  # G:\

DATA_ROOT = _SIBLING_BASE / "Asteria-malf-pas-data"
BACKUP_ROOT = _SIBLING_BASE / "Asteria-malf-pas-backup"
REPORT_ROOT = _SIBLING_BASE / "Asteria-malf-pas-report"
TEMP_ROOT = _SIBLING_BASE / "Asteria-malf-pas-temp"
VALIDATED_ROOT = _SIBLING_BASE / "Asteria-malf-pas-validated"

# --- SQLite 分库 ----------------------------------------------------------
MARKET_DB = DATA_ROOT / "market.sqlite"
MALF_PAS_DB = DATA_ROOT / "malf_pas.sqlite"
BACKTEST_DB = DATA_ROOT / "backtest.sqlite"

# --- TDX 离线数据源 -------------------------------------------------------
TDX_SOURCE_ROOT = Path("H:/tdx_offline_Data")
# 结构识别与回测用后复权；涨跌停判定用不复权原始价。两套都 ingest。
PRICE_LINE_STRUCTURE = "qfq_back"  # Backward-Adjusted
PRICE_LINE_RAW = "raw_none"        # Non-Adjusted
ADJ_FOLDER = {
    "qfq_back": "Backward-Adjusted",
    "raw_none": "Non-Adjusted",
}

# --- 时间分组（硬隔离）----------------------------------------------------
GROUP_YEARS = {
    "initial": (2018, 2019, 2020),
    "validation": (2021, 2022, 2023),
    "holdout": (2024, 2025, 2026),
}

# --- git 远端 -------------------------------------------------------------
GIT_REMOTE_URL = "https://github.com/everything-is-simple/Asteria-malf-pas"

# --- 默认 timeframe（MVP 只做 day）---------------------------------------
DEFAULT_TIMEFRAME = "day"


def ensure_external_dirs() -> None:
    """确保外置目录存在（数据/报告/临时）。"""
    for d in (DATA_ROOT, REPORT_ROOT, TEMP_ROOT):
        d.mkdir(parents=True, exist_ok=True)
