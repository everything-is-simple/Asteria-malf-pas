# 测试与验收设计（TEST_ACCEPTANCE）

> **验收单一权威**。每个里程碑"做到什么程度算完成"的判定标准。
> 治理只有 `pytest + git`——本文件就是验收门，不引入任何 gate/卡片/注册表。
> 原则：**能自动断言的用 pytest；不能的用黄金样本 + 手算对账 + 肉眼核对**。

| 验收层级 | 手段 | 适用 |
|---|---|---|
| L1 单元确定性 | pytest 合成序列断言 | 状态机/查表/规则边界 |
| L2 黄金样本回归 | 固定标的固定时段存期望快照 | 防回归 |
| L3 手算对账 | 人工算 1-2 笔，比对程序输出 | 回测交易语义 |
| L4 肉眼核对 | Streamlit 可视化 | 结构识别质量 |
| L5 无未来函数 | 因果审计 | 回测/管线 |
| L6 端到端 | CLI 全链路跑通 | 里程碑收尾 |

---

## 0. 通用规则

```mermaid
flowchart LR
    DEV[写代码] --> UT[pytest 全绿]
    UT --> GOLD[黄金样本回归]
    GOLD --> E2E[端到端命令跑通]
    E2E --> DONE[里程碑完成]
    DONE -.每步.-> GIT[git commit]
```

- **每个里程碑收尾前 `pytest` 必须全绿**，否则不算完成。
- **不 mock 业务逻辑**：状态机/矩阵/规则用真实实现 + 合成输入断言。
- **黄金样本**：选 2-3 个标的固定时段，存期望快照（pivot/wave/trade），回归时比对。
- **测试无需安装包**：测试头部 `sys.path.insert` 注入 `src`（见 `tests/test_malf_core.py`），或用 `pyproject.toml` 的 `pythonpath=["src"]`。

---

## 1. M1 验收（数据 → MALF Core → 可视化）✅ 已完成

| 项 | 标准 | 状态 |
|---|---|---|
| TDX 解析 | GBK/日期格式/symbol 规范化/board 推断正确 | ✅ |
| 增量灌数 | content_hash 未变则跳过 | ✅ 6/6 跳过验证 |
| Core 单测 | 初始化/break/transition/严格比较 4 测全绿 | ✅ |
| 真实数据核对 | 600000 严格 break 逐条对账 | ✅ 786 bar→34 wave→33 break |
| UI 可视化 | Streamlit 启动 200，figure 构建无误 | ✅ |

**M1 单测覆盖**（`tests/test_malf_core.py`，4 测）：

| 测试 | 验证规则 |
|---|---|
| `test_initial_up_wave_forms_on_H0_L1_H2` | D18 初始化：H0→L1→H2 且 H2>H0 → up_alive，guard/progress 正确 |
| `test_no_wave_when_H2_not_above_H0` | O6：H2==H0 不成 wave，保持 uninitialized，无 break/transition |
| `test_break_terminates_up_wave_and_opens_transition` | D10/D13：严格跌破 guard → break + transition 双边界正确 |
| `test_equality_does_not_break` | O3：bar_low==guard 不构成 break |

---

## 2. M2 验收（MALF Lifespan + v1.5 行为快照）✅ 已完成

> `tests/test_malf_lifespan.py`（12 测）+ `tests/test_malf_behavior.py`（24 测）全绿。

| 测试点 | 标准 | 源 |
|---|---|---|
| new_count | up 只数 HH，down 只数 LL；guard primitive 不计数 | L3 |
| no_new_span | 新波确认 bar=0；推进=0；alive 无推进=+1；terminated 冻结 | L4/L6 |
| transition_span 不并入 | new wave 的 no_new_span 从 0 起，不含 transition bar 数 | L5/L-T3 |
| life_state 判定顺序 | terminated→stagnant→extended→early→developing 优先级正确 | L11 |
| transition 方向保留 | system_state=transition 时 direction=old_direction | L-T5 |
| rank 单调性 | new_count 越大 update_rank 越高（sanity check） | L8 |

### 待补单测 `tests/test_malf_behavior.py`（已完成，24 测）

| 测试点 | 标准 | 源 |
|---|---|---|
| 6 regime 派生确定性 | 同输入→同 regime（查表无随机） | v1.5 C1-C3/L1-L3 |
| transition 优先 | system_state=transition → 不给 continuation bucket | 01B §2 |
| guard 缺失 | 无 current_effective_guard → 不输出 guard_pressure | 01B §2 |
| birth_quality 缺失 | 无 confirmation_distance → 不输出 birth_quality | 01B §2 |

### M2 端到端

```bash
python scripts/run_malf.py --symbol 600000.SH   # 跑出 WavePosition + WaveBehaviorSnapshot
```
**验收**：字段齐全；transition bar 的 direction=old_direction；rank ∈ [0,1]。

---

## 3. M3 验收（PAS v1.5）✅ 已完成

> `tests/test_pas_core.py`（46 测，含 Posture 矩阵全枚举）+ `tests/test_pas_lifespan.py`（14 测）全绿。

### 单测 `tests/test_pas_core.py`（核心：全枚举确定性）

| 测试点 | 标准 | 源 |
|---|---|---|
| Posture 矩阵全枚举 | 5 premise × 5 read_status 全组合输出唯一确定 | PM-T1~6 / C-T4 |
| PM-T1~T5 查表 | 每条定理的五族 posture 与文档逐格一致 | PM-T1~5 |
| PM-T6 降档 | mismatch 时 favored→allowed→deferred→blocked，**只降一次不迭代** | PM-T6/C7 |
| C6 上限约束 | transition/lineage_gap/ambiguity 主导 → 上限 deferred | C6 |
| C6 全 blocked | no_actionable_premise / not_applicable / 缺 lineage → 全 blocked | C6 |
| 三态树阈值 | IA-3 `no_new_span<5`、IA-4 `>=20/>=10` 边界正确 | IA-3/IA-4 |
| 禁止字段 | PASCoreSnapshot 不含三态标签/数值分/accept-reject | C4/Service §8 |

### 待补单测 `tests/test_pas_lifespan.py`

| 测试点 | 标准 | 源 |
|---|---|---|
| 四态转移 | observing→active→submitted/invalidated 条件正确 | L-TR1~5 |
| invalidated 非 Signal 驱动 | Signal rejected 本身不触发 invalidated | L-T3 |
| 新窗口新记录 | invalidated→observing 必须新建 lifespan_id | L-T5 |

### M3 端到端
```bash
python scripts/run_malf.py --symbol 600000.SH --with-pas   # MALF→PAS posture
```
**验收**：每个 bar 输出五族 posture；transition bar 全族受 C6 上限约束。

---

## 4. M4 验收（Signal 质量门 + 结构目标 + 回测引擎）✅ 已完成

> 全套 **189 测试全绿**。M4 相关：signal_engine(24) + signal_structural(10) + backtest_rules(13) + backtest_broker(13) + backtest_engine(11) + backtest_metrics(6) + analyze_run(9)。

### 4.1 Signal 质量门 + 可变 RR `tests/test_signal_engine.py` + `test_signal_structural.py`

| 测试点 | 标准 | 源 |
|---|---|---|
| 7 步判定顺序 | family→posture→质量门→RR→tradable→accept，各 reject 分支正确 | §3 |
| 质量门 2+N | read_status 基本条件 + 5 项评分 + min_quality_score 门槛 | D4 |
| life_state 上限 | terminal/stagnant 被拒（accepted_life_states） | D4 |
| 可变 RR | RR 对结构前高算，无结构退 1.0 < 1.5 拒绝 | D3 |
| 结构 T1/T2 | T1=min(前高,1R)；T2=前高+(前高−guard)；min_risk_pct 地板 | D2/D5 |

### 4.2 仓位规则 `tests/test_backtest_rules.py`（手算对账）

| 测试点 | 标准 | 源 |
|---|---|---|
| 初始止损 | stop = T0.low − stop_offset，floor 到 min_risk_pct×entry | 规则 3 |
| 风险单位 | 1R = entry − stop；结构 T1/T2 | 规则 4 |
| 买入日破止损 | T1 收盘 < stop → T2 开盘清仓 | 规则 5 |
| target1 减半 + 拉保本 | 触及 target1 → 平 50% 且 current_stop ← 入场价 | 规则 6 |
| target2 清剩余 | 减仓后达 target2 → 清剩余 | 规则 6b |
| 保本跟踪 | 减半后按 guard 逐级上移、只上不下、**地板=入场价**（清仓价≥入场价、可低于 target1） | 规则 7 |
| 时间止损 | 自进场无新高累计 time_stop_bars 根 → 退出 | 规则 8 |
| T+1 / 涨停拒买 / 跌停拒卖 | A 股约束（`test_backtest_broker.py`） | 涨跌停 |
| R-multiple | realized_pnl / (1R × original_qty) | 度量 |

### 4.3 L3 手算对账 + L5 无未来函数（`tests/test_backtest_engine.py`）

- 选 1 标的 2-3 笔，手算 entry/stop/结构 T1/T2/减半/保本跟踪/R_multiple，逐字段对账。
- 进场永远在发现日**下一**交易日 open；扫描/结构价/大盘 regime 只读 ≤ bar_dt。

### 4.4 验证基础设施 `tests/test_analyze_run.py`（9 测）

- R 分布分位/直方、exit_reason 占比、target2 命中率、setup_family/read_status 分层、reject 占比。

---

## 5. M5 验收（调参网格 + holdout 锁 + 完整 UI）⏳ 待补

| 测试点 | 标准 | 状态 |
|---|---|---|
| 三组隔离 | initial/validation/holdout 年份不重叠 | ✅ GROUP_YEARS |
| 跨组验证 | initial vs validation 同参并排（`validate_method.py`） | ✅ 已实现 |
| holdout 锁 | holdout 只能跑一次（运行计数锁） | ⏳ 当前靠显式 SystemExit 拦截 + 纪律 |
| 参数网格 | 笛卡尔积每点一 backtest_run（`tuning/grid.py`） | ⏳ 空壳 |
| UI Page1/2 | 机会列表 + 回测并排 | ⏳ 未实现 |

---

## 6. 命令速查（端到端验收）

```bash
pytest                                         # 全部单测（每里程碑必全绿）
pytest tests/test_malf_core.py -v              # 单文件
pytest tests/test_malf_core.py::test_equality_does_not_break -v   # 单测试

python scripts/ingest_data.py --limit 5        # M1 灌数冒烟（stock + index）
streamlit run src/asteria/ui/app.py            # M1+ 肉眼核对结构（Page3）
python scripts/run_backtest.py --symbol 600000.SH --start 2023-01-01 --end 2024-12-31   # M4 单组回测
python scripts/validate_method.py --boards main --limit 200   # 跨组验证（initial vs validation）
python scripts/analyze_run.py --latest-group initial,validation   # 分布分析并排
# python scripts/run_tuning.py                 # M5 参数网格（待补）
```

---

## 7. 黄金样本约定

| 样本 | 内容 | 用途 |
|---|---|---|
| 600000.SH 固定时段 | pivot/wave/break 期望快照 | MALF Core 回归 |
| 2-3 标的固定时段 | bt_trade 期望（entry/stop/R） | 回测回归 |
| 合成序列 | Core/PAS 边界用例 | 确定性回归 |

黄金样本存 `tests/golden/`（待 M2 建），更新需人工确认差异来源（数据变 / 规则变 / bug）后才覆盖。
