"""CLI: TDX txt → market.sqlite。

用法：
  python scripts/ingest_data.py --limit 5      # 只灌前 5 个标的（冒烟测试）
  python scripts/ingest_data.py                # 全量
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让 src 布局可被 import（无需安装）
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from asteria.data import ingest  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest TDX text data into market.sqlite")
    ap.add_argument("--limit", type=int, default=None, help="symbol 数上限（冒烟测试用）")
    args = ap.parse_args()

    summary = ingest.ingest(symbol_limit=args.limit)
    print("Ingest done:")
    print(f"  price_lines    = {summary.price_lines}")
    print(f"  files_seen     = {summary.files_seen}")
    print(f"  files_ingested = {summary.files_ingested}")
    print(f"  files_skipped  = {summary.files_skipped}")
    print(f"  bars_written   = {summary.bars_written}")


if __name__ == "__main__":
    main()
