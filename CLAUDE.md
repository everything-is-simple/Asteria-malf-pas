# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目本质

个人、本地、A 股日线量化 MVP，复刻 MALF/PAS v1.5 形式化规范。**本次重构的核心目的是砍掉上一版（`H:\Malf-Pas`）拖垮开发的治理机械**——施工卡状态机、50+ TOML 注册表、6558 行 checks.py、16 个 DuckDB。这一版治理只有 pytest + git，不要重新引入任何 gate/注册表/卡片机制。

完整设计权威文档：`docs/REBUILD_PLAN.md`。里程碑小结：`docs/M*_SUMMARY.md`。

## 常用命令

```bash
pip install -e .[dev]                         # 安装（src 布局）
pytest                                        # 全部测试
pytest tests/test_malf_core.py::test_xxx -v   # 单个测试
python scripts/ingest_data.py --limit 5       # 灌数冒烟
streamlit run src/asteria/ui/app.py           # UI
```

测试用 `pyproject.toml` 的 `[tool.pytest.ini_options]` 配置 `pythonpath=["src"]`，故测试里直接 `from asteria...`。脚本/UI 用 `sys.path.insert` 注入 `src` 和仓库根（见 `scripts/ingest_data.py`），以便 `from config import settings`。

## 分层架构（严格单向：data → malf → pas → signal → backtest）

这是规范定义的职责边界，**必须在代码层强制，不可越界**：

- **MALF**（`src/asteria/malf/`）只产结构事实。不输出强弱分、setup、accept/reject、交易动作。
- **PAS**（`src/asteria/pas/`）只读 MALF 的 WavePosition + WaveBehaviorSnapshot，产 usage posture（5 族 × 4 档）。**禁读 PriceBar，禁重算 MALF**。
- **Signal**（`src/asteria/signal/`）唯一做 accept/reject + 风报比。不回写上游。
- **回测**（`src/asteria/backtest/`）唯一拥有仓位/订单/成交/盈亏语义。
- `storage` 被各层调用但不反向依赖；`ui` 只读 storage。

每层 `types.py` 是纯数据契约（dataclass + 枚举），无副作用，最易测。

## MALF Core 状态机（系统心脏，最不能返工）

`malf/core.py` 的 `CoreEngine` 逐 bar 推进，**事件顺序固定（O2，9 步）**：ingest → confirm pivots → update progress/guard → break → open transition → update candidate → confirm progress → create new wave → snapshot。

关键不变量（改动前务必理解，否则破坏正确性）：

- **严格比较（O3）**：break/confirmation 用 `<`/`>`，等于不触发。价格比较前先 `normalize_price`（round 2 位，`pivot.py:PRICE_DP`）。
- **break 逐 bar 用 bar.low/high 评估**：极值 bar 早于其确认 bar k 根，故 break 天然先于「违反 guard 的 pivot 确认」触发——这是无未来函数的关键。
- **transition flip-flop（T5/O4）**：处理 transition 内新 pivot 时，先判它是否确认现有 active candidate（D16 的 "after"），不确认才让它成为新 active candidate。
- **guard 唯一性（D9）**：HH/LL 只更新 progress_extreme；只有后续确认的 HL/LH 才替换 current_effective_guard。
- **初始化（O6）**：结构不足时保持 uninitialized，**绝不**产生 break/transition。

pivot 检测（`malf/pivot.py`）用分形确认（fractal-k），确认有 k 根延迟。规则版本 `fractal-k{k}-v1` 必须随快照记录（replay 可追溯）。

## 数据约定

- **复权双轨**：结构识别与回测用后复权（`qfq_back`，连续不跳空）；涨跌停判定用不复权原始价（`raw_none`）。两套都 ingest，`price_bar.price_line` 区分。
- **symbol 格式**：`600000.SH` / `300001.SZ` / `920000.BJ`。board 从代码前缀推断（`data/universe.py:infer_board`），用于涨跌停比例。
- **TDX 源目录**：实际是 `stock/<Adj>/`（非上一版的 `stock-day/`）；GBK 编码；北交所为 `BJ#920xxx`。

## 存储

SQLite WAL，三库分离（`storage/db.py`）：`market` / `malf_pas` / `backtest`。schema 在 `storage/schema.sql`，用 `-- @db: <name>` 注释分段，建库时按段执行。UI 用 `connect_ro()` 只读连接避免写锁争用。各层输出 append-only 快照。
