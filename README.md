# Asteria-Malf-Pas

个人、本地、A 股日线量化 MVP。基于 MALF/PAS v1.5 形式化规范，自写事件循环回测，SQLite 存储，Streamlit UI。

## 设计取舍

- **MALF** 只产结构事实（pivot / HH-HL-LL-LH / wave / break / transition + 6 行为 regime）。
- **PAS** 只产 usage posture（5 setup 族 × 4 档），不做 accept/reject。
- **Signal** 才做 accept/reject + 风报比。
- **回测层** 才管仓位/订单/成交（A 股 T+1、集合竞价、涨跌停）。
- **治理极简**：只有 pytest + git。无施工卡、无注册表、无 checks.py。

数据流：`TDX txt → SQLite(market) → MALF → PAS → Signal → Backtest → SQLite(results) → Streamlit UI`

完整设计见 [`docs/03-task-breakdown/REBUILD_PLAN.md`](docs/03-task-breakdown/REBUILD_PLAN.md)，里程碑进度见 `docs/04-implementation-records/M*_SUMMARY.md`。

## 环境

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e .[dev]           # pandas / numpy / streamlit / plotly / pytest
```

要求 Python ≥ 3.11。

## 目录布局

代码仓库与数据/备份/报告/临时为**同级兄弟目录**，数据文件（`*.sqlite`）严禁进入仓库：

| 用途 | 目录 |
|---|---|
| 代码仓库 | `G:\Asteria-malf-pas` |
| 数据 (SQLite) | `G:\Asteria-malf-pas-data` |
| 备份 / 报告 / 临时 | `G:\Asteria-malf-pas-{backup,report,temp}` |

TDX 离线源数据：`H:\tdx_offline_Data\stock\{Backward-Adjusted,Non-Adjusted}\`（GBK 编码）。

## 常用命令

```bash
# 灌数（增量按 content_hash 跳过）
python scripts/ingest_data.py --limit 5      # 冒烟：前 5 个标的
python scripts/ingest_data.py                # 全量

# 测试
pytest                                        # 全部
pytest tests/test_malf_core.py -v             # 单文件

# UI（Structure Inspector）
streamlit run src/asteria/ui/app.py
```

## 进度

- **M1 ✅** 数据 → MALF Core → 可视化
- M2 MALF Lifespan + v1.5 六行为 regime
- M3 PAS v1.5（三态树 + posture 矩阵 + 四态机）
- M4 Signal + 事件循环回测
- M5 调参 + 分组回测 + 完整 UI
