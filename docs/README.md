# docs 文档总览

本目录只保留四类必要文档，服务于当前仓库的实现、验证与回看。

## 目录划分

| 目录 | 作用 | 当前内容 |
|---|---|---|
| `01-system-structure/` | 系统级总览、边界、总图 | `ARCHITECTURE`（分层/边界/数据流/三库/进度） |
| `02-module-design/` | 模块设计与单一权威规范 | `MALF` / `PAS` / `Backtest`(含 Signal) / `Data Storage` |
| `03-task-breakdown/` | 实现计划、任务拆解、验收口径 | `REBUILD_PLAN` / `TEST_ACCEPTANCE` |
| `04-implementation-records/` | 里程碑实现记录与阶段总结 | `M1`/`M2`/`M3`/`M4` 小结 + `TRADING_METHOD_REFINEMENT` + `VALIDATION_FINDINGS` |

## 当前阅读顺序

1. 先看 `01-system-structure/ARCHITECTURE.md`：分层架构、边界、数据流全局图。
2. 再看 `03-task-breakdown/REBUILD_PLAN.md`：理解这次重构为什么这样做。
3. 然后看 `02-module-design/`：确认各层职责与不可越界边界。
4. 最后看 `04-implementation-records/`：确认哪些已真实完成并验证——**尤其 `VALIDATION_FINDINGS.md` 如实记录第1套方法尚未达稳健**。

## 文档裁剪原则

- 只保留当前仓库真正需要的文档，不复制上一版 `H:\Malf-Pas` 的重治理结构。
- 一类信息只放一个主入口，避免多份文档重复描述同一事实。
- 设计文档写“稳定边界与实现铁律”，阶段总结写“已经做成什么并如何验证”，不要互相混写。
- 能用表和图说明的，不扩成大段散文。

## 上一版错误，这一版不要再犯

- 不重新引入施工卡、注册表、`checks.py`、执行四件套之类的重治理机制。
- 不把“文档完整”误当成“系统已经完成”。
- 不把同一套内容拆成过多平行文件，制造交叉引用负担。
- 不为了“看起来体系化”而先写一大批空壳文档。
