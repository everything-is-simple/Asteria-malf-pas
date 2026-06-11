# M1 阶段小结：数据 → MALF Core → 可视化

日期：2026-06-11
状态：已完成并验证

## 目标

打通最底层的反馈闭环：把 TDX 离线数据灌进库，跑 MALF 结构状态机，把识别出的
pivot/wave/break 画在 K 线上肉眼核对。这是上一版最缺的一环——能跑、能看、能核对。

## 本质区别：没有 block

这一版重构和上一版最本质的区别是**没有任何 block**。没有施工卡、没有治理注册表、
没有 `checks.py`。每一步都是：写代码 → 跑测试 → 跑真实数据核对，一路畅通到底。
治理只保留 pytest + git。

## 已交付且验证

| 层 | 模块 | 验证 |
|---|---|---|
| 数据 | TDX 解析器（修正 `stock/<Adj>/` 路径、北交所）+ ingest + loader | 6278 bar 解析正确，GBK/board/增量跳过全通 |
| 存储 | SQLite WAL 三库 + schema | 建库幂等，分库标记修复 |
| MALF Core | pivot 分形检测 + 状态机（D8-D18 / T1-T10 / O2-O6） | 4 单测 + 600000 真实数据严格 break 逐条核对 |
| UI | Streamlit Structure Inspector | server 启动 200，figure 62 trace 构建无误 |

## 关键技术决策落地

- **结构层用后复权**（连续不跳空，break/guard 比较不被除权污染）。
- **严格比较**（O3，等于不触发 break/confirmation），价格先归一化到 2 位小数。
- **外置数据目录**（`G:\Asteria-malf-pas-data`），数据文件不进仓库。
- **两套 price_line 并存**：`qfq_back`（结构）+ `raw_none`（涨跌停判定，后续 M4 用）。

## 真实数据核对（600000 浦发银行）

786 bar（2023-2026）→ 214 pivot → 34 wave → 33 break → 33 transition，wave 自然交替。
严格 break 逐条核对正确，例如：

- old#30 up，guard=160.94，break low=160.43 < 160.94 ✓
- old#31 down，guard=145.25，break high=145.42 > 145.25 ✓

## 怎么用

```bash
python scripts/ingest_data.py --limit 5    # 冒烟灌数
python scripts/ingest_data.py              # 全量灌数
pytest                                      # 跑测试
streamlit run src/asteria/ui/app.py         # 打开结构可视化，选 600000 肉眼核对
```

## 下一步：M2

MALF Lifespan（new_count/no_new_span/rank/life_state/birth descriptors）
+ v1.5 六个行为 regime → WaveBehaviorSnapshot。
