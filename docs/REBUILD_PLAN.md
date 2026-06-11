# Asteria-Malf-Pas MVP 实现设计 (v1.5)

> 本文件是经批准的重构计划，存放于项目目录作为实施的唯一参照。
> 同步副本：`C:\Users\Administrator\.claude\plans\g-asteria-malf-pas-block-jaunty-nebula.md`（plan mode 临时产物）。

## Context（为什么做这次重构）

上一版 `H:\Malf-Pas` 能跑、能过 block，却经历了几十次返工。根因诊断清楚了：**不是 AI 能力不够，也不是 MALF/PAS v1.5 领域逻辑本身重**，而是外围治理机械严重过载——`checks.py` 单文件 6558 行、50+ 个 TOML 注册表、74 张施工卡状态机、16 个 DuckDB 文件、文档/代码比 7:1、单文件 500 行硬上限。每改一步都要手动同步注册表 → 治理校验失败 → 补文档 → 文档超行要拆 → 改交叉引用，陷入"block / 还能通过 / 继续开发"的死循环。

本次在全新空目录 `G:\Asteria-malf-pas` 重构。目标产出：一个**能跑起来的、个人的、本地的**量化 MVP——能回测、可存储回测结果、能根据历史回测调教系统、能根据 signal + 风报比发现正预期交易机会，并有简单 UI。

**四个已拍板的关键决策：**
1. MALF/PAS **忠实复刻完整 v1.5 形式化规范**（领域逻辑是有价值的部分，保留）。
2. 回测引擎**自写小型事件循环**（用户规则太 A 股特化，现成框架反而要硬掰）。
3. 治理**砍掉重治理，只留 pytest + git**（这是上一版被拖垮的根源）。
4. 股票池**全 A 股，剔除 ST / 上市<1年 / 低流动性**。

数据/存储：DuckDB 单线程难用 → 退回 **SQLite (WAL)**。时间分组：initial=2018-2020，validation=2021-2023，holdout=2024-2026。

数据流主线：
```
TDX txt → SQLite(daily_bar) → MALF(structure facts + WavePosition + WaveBehaviorSnapshot)
   → PAS(PASCoreSnapshot + PASLifespanRecord) → Signal(accept/reject + R:R)
   → Backtest(event loop: position/order/fill/trade) → SQLite(results) → Streamlit UI
```

---

## 核心立场（贯穿全文的取舍）

1. **MALF 只产结构事实**：pivot/HH-HL-LL-LH/wave/break/transition/candidate/confirmation（v1.4）+ 6 个行为 regime 快照（v1.5）。不输出强弱分、不输出 setup family、不输出 accept/reject、不输出任何交易动作。
2. **PAS 只产 usage posture**：消费 MALF 的 WavePosition + WaveBehaviorSnapshot，内部跑三态优先级树（不发布），对外只发布 DirectionalPremise + ReadStatus + EvidenceTriplet + 5族×4档 SetupPostureByFamily，以及 opportunity window 四态生命周期。
3. **Signal 才做 accept/reject**：独立裁决，读 PAS posture + 自己的风报比规则。不回写 PAS/MALF。
4. **回测层才做仓位与执行**：position sizing、止损、减半、移动止损、时间止损、A股 T+1/涨跌停/集合竞价。
5. **治理极简**：只有 pytest + git。所有 "rule_version/lineage/source_run_id" 等字段在 MVP 里保留为**普通数据列**（用于可复现和调试），但**不**建治理注册表、不建 checks.py、不强制四件套。

---

## 1. 架构总览（分层职责与数据流）

```
┌─────────────────────────────────────────────────────────────────────┐
│ L0 Data        TDX txt(GBK,前/后/不复权) → ingest → SQLite daily_bar   │
│                职责: 解析、规整、复权选择、交易日历、universe          │
│                输出: DailyBar 序列 (symbol,bar_dt,o/h/l/c,vol,amount)  │
├─────────────────────────────────────────────────────────────────────┤
│ L1 MALF-Core   逐 bar 推进的结构状态机                                 │
│   (v1.4)       pivot检测 → HH/HL/LL/LH → wave → current effective      │
│                guard → break → transition(双边界) → candidate guard    │
│                → progress confirmation → new wave                      │
│                输出: CoreStateSnapshot (确定性结构事实)                 │
├─────────────────────────────────────────────────────────────────────┤
│ L1b MALF-Life  在已确认 wave 上做统计                                  │
│   (v1.3/1.4)   new_count / no_new_span / update_rank / stagnation_rank │
│                / life_state / position_quadrant / birth descriptors    │
│                输出: WavePosition                                      │
├─────────────────────────────────────────────────────────────────────┤
│ L1c MALF-v1.5  纯派生：把上面事实翻译成 6 个行为 bucket               │
│                continuation/boundary_pressure/directional_continuity/  │
│                stagnation/transition/birth_quality                     │
│                输出: WaveBehaviorSnapshot (只读、非决策)               │
├─────────────────────────────────────────────────────────────────────┤
│ L2 PAS-Core    输入=WavePosition+WaveBehaviorSnapshot ONLY (不读价格)  │
│   (v1.5)       内部三态优先级树(Denying→Proving→Neutral)→DirectionalPremise│
│                → ReadStatus → Posture Matrix(PM-T1~T6) → 5族posture     │
│                输出: PASCoreSnapshot                                    │
├─────────────────────────────────────────────────────────────────────┤
│ L2b PAS-Life   opportunity window 四态机                               │
│   (v1.5)       observing/active/invalidated/submitted                  │
│                输出: PASLifespanRecord                                  │
├─────────────────────────────────────────────────────────────────────┤
│ L3 Signal      独立 accept/reject + 风报比计算                         │
│                读 5族posture + premise + read_status，应用用户规则      │
│                输出: SignalDecision (accepted/rejected, R:R, 进场计划)  │
├─────────────────────────────────────────────────────────────────────┤
│ L4 Backtest    事件循环引擎 (A股特化)                                  │
│                T+0扫描→T+1集合竞价进场→止损/减半/移动止损/时间止损       │
│                T+1/涨跌停/集合竞价约束                                   │
│                输出: Trade/Position/Order/Fill + equity curve          │
├─────────────────────────────────────────────────────────────────────┤
│ L5 Storage     SQLite (WAL). 各层快照表 + 回测结果 + 参数组            │
├─────────────────────────────────────────────────────────────────────┤
│ L6 UI          Streamlit: 机会列表 / 回测结果 / 单标的结构可视化       │
└─────────────────────────────────────────────────────────────────────┘
```

边界铁律（来自文档，必须在代码层强制）：
- MALF 不引用 PAS/Signal；PAS 不读 PriceBar、不重算 MALF；Signal 不回写；回测不回写上游。
- 每层输出 append-only 快照 + 一个 `*_latest` 视图（读加速，非新语义）。
- `system_state` 与 `wave_core_state` 永不混用（MALF S5）。
- 严格突破比较（`<` / `>`，等于不算），价格先归一化精度再比较（MALF O3）。

---

## 2. 目录 / 模块结构（G:\Asteria-malf-pas）

```
G:\Asteria-malf-pas\
  pyproject.toml              # 依赖: pandas, numpy, streamlit, plotly, pytest. 无治理框架
  README.md
  config\
    settings.py               # 路径、universe、复权模式、时间分组等全局常量
    params_default.toml        # 默认参数 (MALF阈值/PAS阈值/Signal/回测) — 唯一的配置文件
  src\asteria\
    data\
      tdx_text.py             # [复用] TDX GBK解析(改 discover 路径 stock/<Adj>/)
      contracts.py            # [复用裁剪] RawMarketBar/TdxSourceFile dataclass
      ingest.py               # txt → SQLite daily_bar；增量按 content_hash
      calendar.py             # 从全市场 bar_dt 并集推交易日历
      universe.py             # A股标的池筛选(剔除指数/退市/ST/上市<1年/低流动性)
      loader.py               # 给上层喂 DailyBar 序列的只读 API
    malf\
      types.py                # Pivot/Primitive/Wave/Break/Transition/Candidate dataclass + 枚举
      pivot.py                # 定: pivot 检测规则(zigzag/分形)，带 rule_version
      core.py                 # MALF-Core 状态机 (D1-D18, T1-T10, O1-O8)
      lifespan.py             # new_count/span/rank/life_state/birth descriptors (L1-L18)
      behavior.py             # v1.5 六 regime 派生 → WaveBehaviorSnapshot
      service.py              # 组装 WavePosition + WaveBehaviorSnapshot 快照
    pas\
      types.py                # DirectionalPremise/ReadStatus/Posture/SetupFamily 枚举 + dataclass
      core.py                 # 三态树(IA-1~5)+Posture Matrix(PM-T1~6) → PASCoreSnapshot
      lifespan.py             # 四态机 (observing/active/invalidated/submitted)
      service.py              # PASCoreSnapshot/CandidateRecord/HandoffRecord 组装
    signal\
      types.py                # SignalDecision/Opportunity dataclass
      engine.py               # accept/reject 裁决 + R:R + 进场计划(止损/target1)
    backtest\
      types.py                # Order/Fill/Position/Trade/Account dataclass
      broker.py               # A股撮合: 集合竞价/涨跌停/T+1 约束
      engine.py               # 事件循环: 逐 bar 推进，调度 signal→order→fill→manage
      rules.py                # 仓位管理: 初始止损/减半/移动止损/时间止损
      metrics.py              # 收益/回撤/胜率/盈亏比/期望
    storage\
      db.py                   # SQLite 连接(WAL)、schema 建表、迁移
      writers.py              # 各层快照/回测结果批量写入
      schema.sql              # 全部建表 DDL
    tuning\
      grid.py                 # 参数网格生成
      runner.py               # 分组回测(initial/validation/holdout) 编排
    ui\
      app.py                  # Streamlit 入口(多页)
      pages\
        opportunities.py      # 机会列表
        backtest.py           # 回测结果
        symbol_view.py        # 单标的结构可视化(K线+wave+pivot+posture)
  scripts\
    ingest_data.py            # CLI: 全量/增量 ingest
    run_malf.py               # CLI: 跑 MALF 全市场或单标的
    run_backtest.py           # CLI: 跑一次回测
    run_tuning.py             # CLI: 跑参数扫描
  tests\
    test_malf_core.py         # 用构造序列验证 D/T/O 规则(break/transition/new wave)
    test_malf_lifespan.py
    test_malf_behavior.py
    test_pas_core.py          # 验证三态树+posture矩阵确定性
    test_pas_lifespan.py
    test_signal.py
    test_backtest_rules.py    # 验证止损/减半/移动止损/时间止损/T+1/涨跌停
    test_data_ingest.py
```

**外置兄弟目录布局**（沿用上一版治理：`.sqlite` 等数据产物禁止进仓库根，统一放仓库的同级兄弟目录）：

| 用途 | 目录 |
|---|---|
| 代码仓库 | `G:\Asteria-malf-pas` |
| 数据(SQLite) | `G:\Asteria-malf-pas-data`（`market.sqlite` / `malf_pas.sqlite` / `backtest.sqlite`） |
| 备份 | `G:\Asteria-malf-pas-backup` |
| 报告 | `G:\Asteria-malf-pas-report` |
| 临时(spill/checkpoint) | `G:\Asteria-malf-pas-temp` |
| validated | `G:\Asteria-malf-pas-validated` |

`config/settings.py` 以这套外置根为准；仓库内 `.gitignore` 不需要排除 `data\`（因为数据根本不在仓库里）。

**远端仓库**：`https://github.com/everything-is-simple/Asteria-malf-pas`（全新、未初始化）。M1 收尾时本地 `git init` → 关联此 remote → 首次 push 到新分支。

依赖方向（单向，便于测试）：
`data → malf → pas → signal → backtest`；`storage` 被各层调用但不反向依赖；
`tuning` 编排 backtest；`ui` 只读 storage。每层 `types.py` 是纯数据契约，无副作用，最易测。

---

## 3. MALF v1.5 实现设计

### 3.1 输入与 pivot 检测 (malf/pivot.py)
- 输入：`DailyBar(symbol, bar_dt, open, high, low, close)`。结构层用**后复权(backward)**，保持历史价格连续可比，break/guard 比较不被除权跳空污染。复权模式记入 `pivot_detection_rule_version` 上下文。
- pivot 检测规则**由实现层指定**（文档 D2）。MVP 用 **zigzag/分形确认**：一个 H 在其前后各 k 根 bar 的 high 都不高于它时确认为 H（L 对称）。k 做成参数 `pivot_k`（默认 2）。
- 关键：pivot **确认有延迟**（要等 k 根后才确认），与文档 O2 一致。pivot 确认时间 = 确认 bar 的 dt，但 pivot 价格/位置属于更早的极值 bar。
- 每个 pivot 带 `pivot_seq_in_bar`（同 bar 多 pivot 排序，O2）。
- **确定性规则**：检测算法固定 + `pivot_k` 固定 ⇒ pivot 序列确定。`pivot_detection_rule_version = "fractal-k2-v1"`。

### 3.2 Core 状态机 (malf/core.py) — 确定性
按 O2 固定事件顺序逐 bar 处理（整个 MALF 的心脏）：
```
for each bar:
  1. ingest PriceBar
  2. confirm pivots (调 pivot.py，可能确认 0..n 个)
  3. update active wave progress / current_effective_guard
  4. evaluate break          (D10: 严格 < / >，O3)
  5. if break: terminate old wave, open transition (D12/D13 双边界)
  6. update active candidate guard (D14/D15, O4 latest 替换)
  7. evaluate progress confirmation (D16: 严格突破 boundary)
  8. create new wave if confirmed (D17, T6 双条件)
  9. publish core_state_snapshot (O7)
```
**状态对象** `system_state ∈ {uninitialized, up_alive, down_alive, transition}` (D11)。
**确定性关键点**：
- 初始化 (D18/O6)：`H0→L1→H2 且 H2>H0` 成立才生 initial up wave；不足则保持 uninitialized，绝不在初始化阶段产生 break/transition。
- guard 唯一性 (D9/T3)：HH/LL 更新只动 `progress_extreme`，只有后续确认的 HL 才替换 `current_effective_HL`（LH 对称）。
- break (D10/O3)：`up` 用 `bar_low < current_effective_HL.price`（严格小于，等于不算）。break 必记 8 字段（old_wave_id...break_dt）。
- transition boundary (D13)：旧 up wave → `high=old final HH price`, `low=broken HL price`；旧 down wave 对称。不可用 break bar 的 high/low。
- candidate (O4/T5)：`active_candidate = latest candidate_guard`，新的一出现就替换；记 `candidate_replacement_count`。
- new wave 确认 (T6/T7)：必须 `active_candidate_guard 存在` 且其后 `progress confirmation 突破 boundary`，缺一不可。
- transition 内 pivot 上下文 = `transition_candidate` (O5)，不更新旧 wave。
- 旧波不复活 (T4)：terminated 永不回 alive，只能建 new wave。

**阈值**：Core 层无数值阈值（除 pivot_k 实现层自定）。文档 O3 明确 `epsilon_policy = none_after_price_normalization`。价格先按数据层精度归一化（round 2 位）再比较。

### 3.3 Lifespan (malf/lifespan.py) — 确定性计数 + 样本分位
- `new_count` (L3)：up 数 HH，down 数 LL。`no_new_span` (L4)：自上次 progress update 起的 bar 数；new wave 确认 bar = 0 (L5)；terminated 冻结 (L6)。
- `transition_span` (L14)、`candidate_wait_span` (L15)、`candidate_replacement_count` (L16)、`confirmation_distance_abs/pct` (L17) = birth descriptors。
- **rank (L8/L9)**：`update_rank/stagnation_rank = percentile_rank(..., peer_sample)`。peer_sample = 同 timeframe + 同 direction + 全市场历史已终止 wave（L7）。**MVP 简化**：用全历史已完成 wave 的经验分布预计算分位表，sample_cutoff 必须 ≤ 当前 bar_dt（防前视）。`sample_version` 记录。
- **life_state (L11)** 阈值判定顺序：terminated→terminal；`stagnation_rank>=high_stag`→stagnant；`update_rank>=high_update`→extended；`update_rank<low_update`→early；else→developing。阈值进 params（建议 high_stag=0.8, high_update=0.8, low_update=0.2 起步）。
- `position_quadrant` (L12)：update_rank × stagnation_rank 的高/低组合。

### 3.4 Behavior Snapshot (malf/behavior.py) — v1.5 六 regime, 确定性派生
全部只能从 v1.4 已确认结构事实派生（v1.5 01B 禁止越界清单）。派生顺序固定（01B §1）：读 core state → 读 lifespan → 读 transition/birth lineage → 派生 bucket → 附 audit reason → 发布。

| regime | 取值 | 来源字段 | 文档 |
|---|---|---|---|
| `continuation_regime` | advancing/slowing/stalled/transitioning | system_state, new_count, no_new_span, rank | MALF_01 C1 |
| `boundary_pressure_regime` | continuation_side/guard_pressure/transition_pressure/neutral | 与 guard/boundary 的关系 | MALF_01 C2 |
| `directional_continuity_regime` | same_direction_continuation/opposite_direction_rebirth/transition_unresolved | direction, old_direction, birth_type, system_state | MALF_01 C3 |
| `stagnation_regime` | fresh/watchful/stalled/terminal_pressure | no_new_span, stagnation_rank, life_state | MALF_02 L1 |
| `transition_regime` | clean_handoff/replacement_heavy/prolonged_unresolved/not_applicable | transition_span, candidate_replacement_count, open_transition_id | MALF_02 L2 |
| `birth_quality_regime` | clean_birth/negotiated_birth/costly_birth/unknown_birth | candidate_wait_span, candidate_replacement_count, confirmation_distance_abs/pct | MALF_02 L3 |

比较铁律 (01B §2)：transition 优先于一切延续 bucket；`system_state != transition` 才给 continuation bucket；无 guard 不输出 guard_pressure；无 `confirmation_distance_*` 不输出 birth_quality。bucket 分界阈值文档未给具体数值 → 放 params。

**WaveBehaviorSnapshot 字段** (MALF_03 §2)：identity(`symbol/timeframe/bar_dt/service_version`) + lineage(`source_run_id/lineage_hash/rule_versions`) + wave linkage(`wave_id/direction/old_wave_id/open_transition_id`) + 6 regime + audit(`reason_codes`)。`WaveBehaviorSnapshotLatest` = 每 symbol/timeframe 最新一条。

### 3.5 Service (malf/service.py)
组装 `WavePosition`（MALF_03 v1.4 S3-S9）与 `WaveBehaviorSnapshot`，作为 PAS 的唯一输入 bundle。只读，不发明语义，不输出 buy/sell/weight/order (S11)。

---

## 4. PAS v1.5 实现设计

### 4.1 定位
PAS = MALF 的 **usage policy layer**。输入只有 `WavePosition + WaveBehaviorSnapshot`（公理 A1，C8 禁读 PriceBar）。输出只有 "当前适合用什么 setup"（posture），不做 accept/reject。

### 4.2 PAS Core (pas/core.py) — 确定性流水线
事件顺序固定 (PAS_01B C2)：校验 input contract → 内部三态树(IA-1~4，不发布) → IA-5 映射 DirectionalPremise → 收集 EvidenceTriplet → 确定 ReadStatus → Posture Matrix(PM-T1~6) → 发布 PASCoreSnapshot。

**内部三态树 (IA-1~4)** — 全确定性按顺序：
- **Denying**（任一成立，IA-2）：`system_state=transition` / guard broken / `life_state=terminal` / `continuation_regime=transitioning` / `stagnation_regime=terminal_pressure` / `boundary_pressure_regime=guard_pressure`。
- **Proving**（8 条全部成立，IA-3）：`system_state∈{up_alive,down_alive}` ∧ guard intact ∧ `new_count>=1` ∧ `life_state∈{early,developing}` ∧ `no_new_span<5` ∧ `continuation_regime=advancing` ∧ `stagnation_regime∈{fresh,watchful}` ∧ `boundary_pressure_regime=continuation_side`。
- **Neutral subtype**（取编号最小，IA-4）：1 terminal_observation(no_new_span>=20) / 2 stagnant(no_new_span>=10 或 stagnation_regime=stalled) / 3 slowing(continuation_regime=slowing) / 4 newborn(new_count=0) / 5 watchful(其余)。
- 注意：IA-3 的 `no_new_span<5`、IA-4 的 `>=20/>=10` 是文档**写死的硬阈值**，照搬不可改。

**IA-5 映射表**（查表，C5）：Denying→`expect_weakness_rejection`(weak)；Proving→`expect_strength_continuation`(strong)；Neutral.terminal_observation→`no_actionable_premise`；Neutral.stagnant→`expect_boundary_test`；Neutral.slowing→`expect_boundary_test`；Neutral.newborn→`no_actionable_premise`；Neutral.watchful→`expect_transition_resolution`。

**ReadStatus (C4)**：`strong/weak/mixed/ambiguous/not_applicable`，由 EvidenceTriplet 主导成分决定。

**Posture Matrix (PM-T1~T6)** — 确定性查表 (DirectionalPremise, ReadStatus) → 五族 posture：
| 定理 | 触发条件 | TST | BOF | BPB | PB | CPB |
|---|---|---|---|---|---|---|
| PM-T1 | strength_continuation + strong | allowed | blocked | favored | favored | deferred |
| PM-T2 | weakness_rejection + weak | allowed | favored | blocked | blocked | deferred |
| PM-T3 | boundary_test + mixed | favored | allowed | deferred | deferred | deferred |
| PM-T4 | transition_resolution + ambiguous | deferred | deferred | blocked | blocked | blocked |
| PM-T5 | no_actionable_premise 或 not_applicable | blocked | blocked | blocked | blocked | blocked |
| PM-T6 | ReadStatus 与 Premise 不匹配 | 全体降一档(favored→allowed→deferred→blocked)，只降一次 |

Posture 上限约束 (C6)：transition_bound / lineage_gap / ambiguity 主导 → 上限 deferred；无 lineage / premise=no_actionable → 全 blocked。

**PASCoreSnapshot 字段** (C9/PAS_03 §3)：identity + lineage + PASContext + DirectionalPremise + ReadStatus + EvidenceTriplet(三数组) + 五族 posture + judgment_reason + posture_derivation_reason + premise_mapping_branch + posture_theorem_branch + rule_versions。**禁止字段**：三态标签、数值分数、accept/reject/buy/sell/order/position/fill/profit。

### 4.3 PAS Lifespan (pas/lifespan.py) — 四态机
状态 (L1)：`observing / active / invalidated / submitted`。
- L-TR1 observing→active：PASCoreSnapshot 发布 ∧ 至少一族 posture∈{favored,allowed}。
- L-TR2 active→submitted：Signal 接收候选 ∧ HandoffRecord 已记。
- L-TR3 active→invalidated：新快照该 family posture 变 blocked / premise 反转 / MALF guard broken 或 transition。
- L-TR4 submitted→invalidated：Signal rejected 且同时触发 L-TR3 条件（Signal rejected 本身不触发 invalidated）。
- L-TR5 invalidated→observing：新快照 posture 重新满足 → 必须新建 PASLifespanRecord(新 lifespan_id)，不复用旧 record。
铁律：invalidated 由 MALF/Core 驱动，不由 Signal 裁决驱动；submitted ≠ accepted。

### 4.4 PAS Service (pas/service.py) + Signal Feedback
- Service 只读发布：PASCoreSnapshot(Latest)、PASCandidateRecord(Latest)、PASLifespanRecord、PASServiceHandoffRecord。
- SignalFeedback (PAS_04)：`signal_decision∈{accepted,rejected}` + reason + version，**只用于 audit/统计/replay，绝不回写** Core/Lifespan/MALF。

---

## 5. Signal + 回测引擎设计

### 5.1 职责切分
- **Signal**：消费 PAS posture + 自身规则做 accept/reject（独立裁决）。MVP 规则：某 family posture∈{favored,allowed} ∧ 风报比达标 ∧ A 股可交易 → accept，产出 `SignalCandidate`（含进场/止损/目标计划值）。
- **回测引擎**：唯一拥有仓位、订单、成交、盈亏语义的层。逐 bar 事件循环，执行用户 A 股特化规则。

### 5.2 用户交易规则形式化（T0=机会发现日收盘扫描，T1=进场日）
1. **机会发现**：T0 收盘后扫描，Signal accept → 生成挂单意图。
2. **进场**：T1 开盘集合竞价执行（买/卖）。
3. **初始止损**：`stop = T0.low - 0.02`。
4. **风险单位**：`1R = entry_price - stop`；`target1 = entry_price + 1R`。
5. **买入日破止损**：若 T1 收盘 < stop → T2 开盘清仓。
6. **达 target1**：减仓 50%。
7. **移动止损**：剩余仓位移动止损不断上移，触及即清仓；**最终移动止损必须高于 target1**。
8. **时间止损**：`time_stop_bars` 内价格不动/趋势消失 → 退出。
9. **A 股约束**：T+1；涨停无法买入、跌停无法卖出；集合竞价撮合。

### 5.3 涨跌停判定
主板 ±10%、ST ±5%、创业板/科创板(300/688/30/68) ±20%、北交所(8/4/920) ±30%。以前收盘价 round(prev_close×(1±limit),2) 为限价。MVP 简化：`open>=up_limit` 无法买入；`open<=down_limit` 无法卖出（集合竞价价=open）。board 精确化作为参数后置。

### 5.4 事件循环 (backtest/engine.py)
```
for bar_dt in trading_calendar[start:end]:
    1. 结算 pending orders（本 bar 集合竞价/open 撮合，检查涨跌停）
    2. 更新 open Position：算 1R 进度、target1 减半、移动止损上移、时间止损递增
    3. 触发平仓单（止损/移动/时间/买入日破线）→ next-bar 卖出意图（T+1）
    4. 喂 bar 给 MALF→PAS→Signal 管线（截至本 bar，无未来函数）
    5. Signal accept → 生成 T+1 买入意图，挂 pending orders
    6. 记录组合净值快照
```
逐 bar 严格因果：扫描只用 <= bar_dt 的数据；进场永远在发现日的下一交易日 open。

### 5.5 关键数据结构 (backtest/types.py, dataclass)
```
Order:    order_id, symbol, side, order_type(moo), intended_dt,
          reason(entry/stop/target1/trailing/time_stop/breakdown),
          qty, limit_ref, status(pending/filled/rejected)
Fill:     fill_id, order_id, symbol, fill_dt, fill_price, qty,
          reject_reason(limit_up/limit_down/halt/none)
Position: position_id, symbol, direction, entry_dt, entry_price, qty,
          initial_stop, risk_unit_R, target1, current_stop(trailing),
          half_exited(bool), bars_held, status(open/closed)
Trade:    trade_id, symbol, entry_dt, exit_dt, entry_price, avg_exit_price,
          qty, realized_pnl, R_multiple, exit_reason, signal_candidate_id
```
R-multiple = realized_pnl / (risk_unit_R × original_qty)，调参/统计核心度量。

---

## 6. SQLite Schema 设计

### 6.1 连接策略
- DB 在 `G:\Asteria-malf-pas-data\*.sqlite`（外置兄弟目录，不在仓库内）。
- WAL：`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`（并发读 + 单写）。
- 单写者：管线/回测串行写，UI 只读连接（`mode=ro`）。`storage/db.py` 统一 connect()，启用 WAL + foreign_keys。
- 分库：`market.sqlite`（行情）、`malf_pas.sqlite`（结构/posture 快照）、`backtest.sqlite`（回测结果/参数组）。

### 6.2 行情库 market.sqlite
```
instrument(symbol PK, name, exchange, board, first_dt, last_dt, list_status)
price_bar(symbol, bar_dt, price_line, open, high, low, close, volume, amount,
          PRIMARY KEY(symbol, bar_dt, price_line))   -- price_line: 'qfq_back'/'raw_none'
trade_calendar(trade_date PK, is_open)
```

### 6.3 结构/posture 库 malf_pas.sqlite（append-only 快照）
```
malf_core_snapshot(snapshot_id PK, symbol, timeframe, bar_dt, system_state,
   active_wave_id, old_wave_id, direction, wave_core_state,
   current_effective_guard_pivot_id, current_effective_guard_price,
   progress_extreme_pivot_id, progress_extreme_price, open_transition_id,
   active_candidate_guard_pivot_id, active_candidate_direction,
   transition_boundary_high, transition_boundary_low,
   core_rule_version, pivot_detection_rule_version, source_run_id,
   UNIQUE(symbol,timeframe,bar_dt,source_run_id))

wave_position(wave_position_id PK, symbol, timeframe, bar_dt, wave_id, old_wave_id,
   system_state, wave_core_state, direction, new_count, no_new_span, transition_span,
   update_rank, stagnation_rank, life_state, position_quadrant, birth_type,
   candidate_wait_span, candidate_replacement_count,
   confirmation_distance_abs, confirmation_distance_pct,
   sample_version, lifespan_rule_version, source_run_id)

wave_behavior_snapshot(wave_behavior_snapshot_id PK, symbol, timeframe, bar_dt,
   wave_id, direction, old_wave_id, open_transition_id,
   continuation_regime, directional_continuity_regime,
   stagnation_regime, boundary_pressure_regime, transition_regime, birth_quality_regime,
   reason_codes, lineage_hash, malf_v1_4_rule_version, malf_v1_5_rule_version, source_run_id)

pas_core_snapshot(core_snapshot_id PK, symbol, timeframe, bar_dt,
   malf_wave_position_id, wave_behavior_snapshot_id, directional_premise, read_status,
   strength_evidence, weakness_evidence, ambiguity_evidence,
   tst_posture, bof_posture, bpb_posture, pb_posture, cpb_posture,
   judgment_reason, posture_derivation_reason, premise_mapping_branch,
   posture_theorem_branch, pas_core_rule_version, source_run_id)
   -- 内部三态字段不入库

pas_lifespan_record(lifespan_id PK, symbol, timeframe, bar_dt, core_snapshot_id,
   setup_family, lifespan_state, state_reason, pas_core_rule_version,
   malf_wave_position_id, wave_behavior_snapshot_id, lifespan_rule_version)
pas_lifespan_transition(id PK, lifespan_id, from_state, to_state, trigger,
   trigger_reason, bar_dt, core_snapshot_id_at_transition)
```

### 6.4 回测库 backtest.sqlite
```
param_set(param_set_id PK, name, params_json, created_at)
backtest_run(run_id PK, param_set_id, group_name, start_dt, end_dt,
   universe_filter, created_at, status)
bt_trade(trade_id PK, run_id, symbol, entry_dt, exit_dt, entry_price,
   avg_exit_price, qty, realized_pnl, R_multiple, exit_reason, signal_candidate_id)
bt_equity_curve(run_id, bar_dt, equity, cash, open_positions, PRIMARY KEY(run_id,bar_dt))
bt_metrics(run_id PK, total_return, cagr, max_drawdown, sharpe, win_rate,
   avg_R, expectancy, trade_count, profit_factor)
signal_candidate(signal_candidate_id PK, run_id, symbol, discover_dt, setup_family,
   directional_premise, read_status, planned_entry, planned_stop, planned_target1,
   reward_risk, decision(accept/reject), reason)
```

---

## 7. 调参 / 分组回测设计

### 7.1 时间分组（硬隔离）
| 组 | 年份 | 用途 |
|---|---|---|
| initial (train) | 2018, 2019, 2020 | 参数网格扫描，自由调参 |
| validation (calib) | 2021, 2022, 2023 | 校对，挑选 top-N 参数组 |
| holdout (reserve) | 2024, 2025, 2026 | 最后一次性验证，禁止迭代 |

铁律：holdout 整个调参过程**只能跑一次**。`tuning/grid.py` 对 holdout 加运行计数锁。

### 7.2 参数网格（全部来自 config）
pivot_lookback；stop_offset（默认 0.02）；time_stop_bars（5/8/13）；trail_method（chandelier/prev_HL/ATR×k）+ trail_k；target_R（默认 1.0）+ scale_out_pct（默认 0.5）；signal filter（接受哪些 posture + min_reward_risk）；universe filter（最小流动性/上市天数）。

### 7.3 工作流（tuning/runner.py）
1. initial 组笛卡尔积扫描（每点一 backtest_run）。
2. 用 bt_metrics（expectancy/profit_factor/max_dd）排序选 top-N。
3. validation 组重跑 top-N，剔除过拟合。
4. 选定唯一参数组 → holdout 跑一次 → 最终报告。
- 串行写库（SQLite 单写）；可 multiprocessing 并行算 param 点，主进程串行 flush。

---

## 8. Streamlit UI 设计（最小可用，只读连接）

- **Page 1 机会列表**：选日期 → 查 signal_candidate；表格 symbol/setup_family/premise/read_status/五族posture/entry/stop/target1/reward_risk/decision；按 reward_risk 排序、按 family 过滤。
- **Page 2 回测结果**：选 run → bt_metrics 卡片 + equity curve + trade 列表 + R 分布直方图；initial/validation/holdout 三组并排对比。
- **Page 3 单标的结构可视化**：选 symbol + 日期范围 → plotly candlestick，叠加 pivot/wave/guard 线/transition 带/break 标记；下方面板逐字段显示 WavePosition + 六 regime + PAS 五族 posture。**这是验证 MALF 正确性的核心调试视图，M1 就要能用。**

---

## 9. 分阶段实施路线（里程碑）

- **M1 数据→MALF Core→可视化（最高优先）**：移植 TDX 解析器（修 `stock/<Adj>/` 路径）灌 market.sqlite；pivot 检测 + Core 状态机；Structure Inspector(Page3)。验证：单标的(600000) K 线上 pivot/wave/break/transition 肉眼正确，event ordering 可重放。
- **M2 MALF Lifespan + v1.5 行为快照**：计数/rank/life_state/quadrant/birth descriptors + 6 regime。验证：字段齐全，transition 保留 old_direction，rank 单调性 sanity check。
- **M3 PAS v1.5**：三态树 → premise → read_status → posture matrix + 四态机。验证：posture matrix 全枚举确定性单测，C6 上限 + PM-T6 降档正确。
- **M4 Signal + 回测引擎**：accept/reject + 事件循环（T+1 集合竞价/止损/减半/移动止损/时间止损/涨跌停）。验证：单标的手算 1-2 笔对账，无未来函数。
- **M5 调参 + 分组回测 + 完整 UI**：参数网格 + 三组工作流 + holdout 锁 + Page1/Page2。验证：initial→validation→holdout 全流程跑通。

---

## 10. MVP 务实取舍

原则：**接口字段保留完整（按规范定义），但派生逻辑可先用桩/简化实现**，后续填充不改接口形状。

### 完整实现（系统正确性的根）
MALF Core 全状态机；MALF v1.5 六 regime；PAS Core 三态树 + posture matrix；回测引擎 A 股交易规则。

### 简化/桩实现
| 规范项 | MVP 取舍 | 接口保留 |
|---|---|---|
| Lifespan rank peer_sample 版本化 | 全市场同方向 + 截至当前 bar 单一样本，sample_version 写死常量 | 保留字段 |
| lineage_hash / 完整 rule_versions 审计链 | 简单字符串常量或 run_id，不做哈希校验 | 保留字段列 |
| replay determinism 正式校验(O8) | 不做自动 replay 审计，靠 pytest 固定快照 | 记录 rule_version |
| PAS reason codes 全文 | 短 enum/分支名 | 保留字段 |
| EvidenceTriplet 详细证据 | 命中 regime 名列表 | 保留三数组 |
| *Latest 物化表 | SQL MAX(bar_dt) 查询 | 后续可加 |
| 多 timeframe(week/month) | 只做 day | timeframe 字段保留 |
| index/block 资产 | 只做 stock | asset_type 字段保留 |

### 明确不做
broker/paper-live/实盘对接（规范禁止）；正式 DB mutation 治理、施工卡、governance registry（本次重构核心目的就是砍掉）。

### 关键技术决策
- **复权**：结构与回测统一用后复权（连续不跳空）；涨跌停判断用不复权原始价另算。ingest 两套都进库（`price_line` 区分 'qfq_back'/'raw_none'）。
- **涨跌停**：用不复权收盘价算次日限价；symbol→board 从代码前缀推断。MVP 先统一 ±10% 近似，board 精确化后置。
- **集合竞价**：MVP 用 T+1 开盘价作成交价；涨停 open=涨停价则买入失败，跌停同理。

---

## 11. 依赖与环境
Python 3.11+；依赖 `pandas/numpy/streamlit/plotly/pytest`；不依赖 duckdb 及任何治理框架；SQLite 用标准库 `sqlite3`(WAL)；包管理 `uv` 或 pip+venv，pyproject.toml 最小化。

## 12. 测试策略（pytest only）
- `test_malf_core.py`：合成 OHLC 序列断言 pivot/wave/break/transition/candidate/confirmation 与手算一致；O3 边界(==不触发)。
- `test_malf_behavior.py`：regime 派生查表正确性。
- `test_pas_core.py`：posture matrix 全枚举(5 premise × 5 read) + PM-T6 降档 + C6 上限。
- `test_backtest_rules.py`：单标的手算对账（进场/止损/减半/移动/时间/涨跌停拒成交）。
- `test_data_ingest.py`：TDX 解析（GBK、日期格式、复权目录）。
- 黄金样本：2-3 个标的固定时段存期望快照，回归测试。

## 验证方式（端到端）
1. M1 后：`python scripts/ingest_data.py` 灌数 → `streamlit run src/asteria/ui/app.py` 打开 Page3，肉眼核对 600000 的结构标注。
2. 每个里程碑跑 `pytest tests/` 全绿。
3. M4 后：`python scripts/run_backtest.py` 跑单组，手算对账 1-2 笔交易的 R 倍数。
4. M5 后：`python scripts/run_tuning.py` 走完 initial→validation→holdout，UI Page2 三组指标并排可见。
