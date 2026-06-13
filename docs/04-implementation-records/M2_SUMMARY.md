# M2 阶段小结：MALF Lifespan + v1.5 行为层 → PAS 完整输入源

日期：2026-06-12
状态：已完成并验证

## 目标

把 MALF 补成 PAS 的唯一合法输入源。M1 的 Core 状态机逐 bar 产 `CoreStateSnapshot`（结构事实），
但 PAS 公理 A1 规定它只能读 `WavePosition + WaveBehaviorSnapshot`、禁读 PriceBar、禁重算 MALF。
M2 就是把这两个契约对象造出来——在已确认 wave 上做生命统计 + 纯派生行为 regime。

## 本质区别：延续 M1，仍然没有 block

和 M1 一样，全程没有施工卡、没有治理注册表、没有 `checks.py`。每一步都是：
写代码 → 跑测试 → 跨版本核对。治理只保留 pytest + git。分三步推进，每步独立可测：
契约补齐 → Lifespan 统计 → v1.5 行为派生。

## 已交付且验证

| 层 | 模块 | 验证 |
|---|---|---|
| 契约 | `types.py` 追加 Lifespan/behavior 枚举 + `WavePosition`(19 字段) + `WaveBehaviorSnapshot`(6 regime) | 字段与 `schema.sql` 表、PAS §2.3 消费需求逐字对齐 |
| Lifespan | `lifespan.py`：计数/rank/life_state/quadrant/birth descriptors | 12 单测，含 rank 单调性、cutoff 防前视、birth 稳定性 |
| 行为层 | `behavior.py`：6 个 regime 纯派生 | 24 单测，覆盖各 bucket + 比较铁律 + 越界禁止 |
| 端到端 | `runner.py` 全链路组装 + `malf_writer.py` 落库 | 3 e2e：字段对齐 + writer 往返 + 幂等 |

合计 **39 个 M2 单测全绿**（lifespan 12 + behavior 24 + e2e 3），叠加 M1 Core 测试无回归。

## 关键技术决策落地

- **两趟算法**（Pass1 计数 + 同向已完成波样本收集；Pass2 回填 rank），分离统计与百分位派生。
- **rank 用本 run 自洽经验分布**：同 timeframe + 同 direction 的已 terminated 波终值入样本，
  `end_bar_dt ≤ 当前 bar_dt` 防前视（L9）。接口预留 `peer_provider` 外部注入点。
- **birth 描述一次定型**（P1 修复）：在 wave 确认 bar 算一次 `confirmation_distance`，
  缓存到整条 wave 生命周期，后续 HH/LL 推进不再改写——否则距离会从「确认 pivot vs boundary」
  漂移成「后续更低 LL vs boundary」。
- **三条派生铁律**（v1.5 01B §2，写死进派生函数前置判定）：
  transition 优先于一切延续 bucket；无 guard 不出 `guard_pressure`；无 distance → `unknown_birth`。
- **越界禁止**：行为快照永不含 strength/setup/accept/order/position/fill/profit（单测断言字段集）。

## 不变量核对（M2 验收点）

| 不变量 | 规则 | 验证 |
|---|---|---|
| transition 保留 old_direction | break 后 transition 期 `direction = old_direction`（L-T5） | `test_transition_keeps_old_direction_and_span_isolated` |
| rank 单调性 | value 越大 → ≤value 占比不减 | `test_percentile_rank_monotonic_nondecreasing` |
| birth 稳定 | 同 wave 的 `confirmation_distance` 跨所有 bar 恒为唯一值 | `test_birth_confirmation_distance_stable_across_wave` |
| terminal 真实产出 | break bar 据 `break_event` 标 terminated → `life_state=terminal`（P3） | `test_terminal_life_state_emitted_on_real_break_bar` |
| transition_span 隔离 | transition 跨度不并入新波 `no_new_span`（L5/L-T3） | 同 L-T5 用例断言 |

## 跨版本核对

实现与上一版 `H:\Malf-Pas` 交叉验证，确认计数/rank/birth 语义一致，
设计正确性得到独立佐证（无 block 也能保证质量）。

## 已记录的妥协（透明留痕）

- `candidate_wait_span`：Core 当前未填精确值 → 本次用 transition 跨度近似 + reason 标注，
  **不改 Core**（避免 M1 返工）。若 PAS 后续对该字段敏感再回补 Core 时间戳。
- `transition_span` 以 bar 数计（非自然日），MVP 口径，已在代码注释标注。

## 怎么用

```bash
pytest tests/test_malf_lifespan.py tests/test_malf_behavior.py tests/test_malf_runner_e2e.py -v
python scripts/run_malf.py --symbol 600000.SH            # 只算 + 打印验收摘要
python scripts/run_malf.py --symbol 600000.SH --write    # 同时落库到 malf_pas
```

## 下一步：M3

PAS（usage posture）：消费 `WavePosition + WaveBehaviorSnapshot`，产 5 族 × 4 档姿态。
禁读 PriceBar、禁重算 MALF——M2 已把输入源备齐。
