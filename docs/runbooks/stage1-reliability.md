# 阶段一可靠性门禁与线上排障

本套件属于流程可靠性证据，全部 Planner/生成服务为规则或 fake，不是内容质量分数。复用评测应用、Core 三道审核、事件处理器/发布器/定时器、生成队列、积分、MediaHost 和画布实现；不走历史 JSON/FIFO 沙箱，也不复制业务状态机。

## 自动门禁

每次 Git 提交（包括文档/空提交）由现有 pre-commit 路径选测器追加相同可靠性命令：

```sh
PYTHONPATH=apps/api .venv/bin/python -m backend.evaluation.reliability.cli --output /absolute/path/report
```

默认要求 Docker；创建带 `aivideo.reliability` 标签的临时 PostgreSQL/RabbitMQ/Redis，仅清理本次创建的容器。没有 Docker 或依赖失败时阻断提交，不静默跳过、不连接应用配置中的数据库。测试文件、输出与生产 data/ 分离。

CI 新增 reliability job，使用独立空测试数据库；执行阶段硬预算 5 分钟，依赖安装另计。失败仍上传 summary.json、JUnit、每场景 trace.json、invariants.json、迁移/契约日志与 fake 数据。CI 与本地用同一入口；`--external` 只允许显式 loopback、空的 reliability_* 数据库，没有生产连接回退。每场景从已迁移空模板克隆独立数据库，并发数、总预算、单场景预算、事件数/并发 Run 数、定时器持锁时间均由 YAML 配置。

调试可指定 `--case scenario_id`；报告 full_suite=false，不作为完整门禁证据。默认全部执行，不能通过环境变量跳过本地门禁。

## YAML 与覆盖

`tests/reliability/stage1.yaml` 为严格 schema，仅允许声明的 probe/checkpoint，不能加载 Python 标签、导入模块或执行自定义命令。包含 37 个场景，合并共同准备的契约断言以保留五分钟余量，没有删除被合并场景的检查：

| 场景 | 覆盖 |
| --- | --- |
| empty_migration_cycle | 独立空库升级→回滚到底→重升 |
| event_integrity | 原块 2 锁内重读、双消费者、清理回执后的重复确认、缺失 Run/回执/游标/尝试/事件隔离告警、发布/接收锁竞争、定时器修复；原持锁计时用例由 timer_locked 的 5 Run/30 秒场景覆盖 |
| core_contracts | 原 Core 数据库状态机全套合法/非法边、CAS 竞争、状态/outbox 同事务；现有集成测试日志独立保存 |
| tool_and_asset_contracts | 参数/类型链、未知错误归一并告警、资产版本及血缘不可覆盖 |
| event_order / event_parallel | 同 Run 100 事件逆序/重复到达仍串行；10 Run 并行，处理效果按 event_id 唯一 |
| dead_replay | 持续异常→死信可查→修复并手动重放 |
| timer_restart / timer_locked | 真进程登记定时器后 SIGKILL，重启触发；同时锁 5 Run 30 秒，其他 Run 定时器继续触发 |
| db_outage | TCP 代理在状态/outbox 写入后断连，验证事务未提交；同一池恢复并继续 |
| queue_backlog / publisher_after_send | 真实 RabbitMQ 暂停接收后恢复；发布后标记前 SIGKILL，重发只生效一次 |
| kill_wake / kill_planner / kill_plan / kill_provider_accepted / kill_t1 / kill_t2 | 唤醒、输出落库、审核入队、服务商接受未存 ID、两次产物提交后的实际 SIGKILL |
| duplicate_callbacks / expiring_transfer | 三次回调；丢回调靠轮询，60 秒链接内失败重试转存 |
| blocked_refund / retry_exhausted / unsupported_idempotency | 拦截不重试退款、有界重试、非幂等服务商丢响应后按登记查询恢复 |
| cancel_true / cancel_false | 确认取消后退款；不可取消跑完进历史并实际结算 |
| canvas_anchor_conflict / second_task_failed | 锚点版本冲突保留原图并落空位；第二步失败保留首步产物 |
| three_review_rejections / round_limit / token_limit | 审核三次失败、思考轮数及 token 限额 |
| single_subject / target_subject / multi_single / multi_all / no_subject / plan_subject | agent 抠图的自动、目标选择、文字选择、并集、无主体、计划模式，模型调用为零 |
| diagnostics_contract | 真 PG 的 API 鉴权/跨工作区隔离、脱敏、分页、指标/告警去重及恢复；故意制造积分/画布/终态 Task 异常并回滚，确认检查器确实会失败 |

所有场景最终校验：每笔预扣=结算+退款，账户余额+累计结算+未结预扣=授予积分；Run 花费与流水一致、无孤儿 Task/依赖、每个画布生成产物有 Run/Task/user 血缘及发布收据、终态 Run 没有活动 Task。失败场景也保存检查结果，不“修复数据”使断言变绿。

不可查询且不支持幂等的未知提交仍需冻结对账，不能假装退款或结束。此类未收敛运行在生产排障用 settled=false 检查；线上查询不要求历史画布收据仍等于被后续合法编辑覆盖后的节点，保留其原始血缘；正式场景最终断言用 settled=true，必须完成对账才通过。

## Trace、指标与告警

`0021` 只补 Planner 开始/返回、审核时间，历史未知时间保留 NULL，不补造。用户消息、调用原始结果、审核、Plan 版本、Task/Attempt、事件及消费尝试由数据库投影串联；带 run_id、parent_run_id、task_id 及 event_id/causation_id/model_call_ref。事件分页以 run_sequence 排序，避免同事务相同时间戳造成乱序；其他段使用时间+id 游标，均可继续翻页。

现有鉴权 `/internal/metrics` 增加窗口化 Run 成功率、各工具按失败类型的失败率、审核退回率、积压、死信（含隔离记录）、退款积分、未知错误数。用户取消不放入成功率分母；分母零为 NaN，不报假 0。退款等为数据库窗口 gauge，不冒充跨清理不变的 counter；工具标签来自已发布目录并限制数量，超限归 other；Run/Task id 不作指标标签。

独立 `core-monitor` daemon 复用 EventAlert 与日志，阈值、窗口、最小样本和告警间隔由 `AIVERO_DIAGNOSTICS_` 配置；重复告警去重，恢复后标记 resolved，不新建外部通知渠道。平台 metrics 继续受原 metrics token 保护。

停止 Run 时若有在途 Task，先 paused(waiting_info)，等工人确认取消或完成实际结算才进入终态。提交响应丢失的 timeout 只触发对账，不被主管误判成已经失败。这两处修复由新场景覆盖。

## 内部排障页

迁移到 `0021`、构建前端并启动新 monitor 服务。应用登录后访问 `/internal/runs`（需要同工作区 owner/admin）；复用现有评测 React 应用，不给历史本地评测进程授予生产数据库权限。本地独立评测页的 Run 排障需要在应用认证域名打开。

输入 Run UUID，可查询所有 Plan、任务/依赖/准入/执行阶段、消息、规划调用和审核、事件序列/消费错误、隔离/关联告警、逐步预扣/结算/退款/未结金额、父子 Run。每段支持分页；历史缺失显示“未记录”，活动 Run 不会被当作已经结清。此页只读，没有重放/退款/执行按钮。

接口为 `/api/internal/core/runs/{id}` 及 `/{section}?after=...`。身份及工作区检查在服务端完成；下载票据、凭据、密钥、蒙版内容/handle、存储路径脱敏，保留标签/置信度及类型引用。原始调用上下文不返回浏览器。不要将此接口改为匿名或绕过工作区权限。

## 本地验收记录

2026-10-22：完整 37 场景通过，默认 Docker 隔离入口总耗时 253.046 秒（包含建库、迁移、清理；同机同时执行其他验证）。`npm run verify` 通过，后端 991 passed / 2 skipped；独立迁移数据库上的生产 API、持久执行、执行工人、主管集成测试共 70 passed。最终交付内容（含排障页面）的独立副本亦通过 `npm run verify`；原块 2 回执/锁回归 16 项与 Core 数据库契约 62 项在 YAML 套件内执行。已配置 CI 相同入口，远端 CI 结果以提交后的工作流为准。

排障接口使用只读、可重复读事务，避免活动 Run 在多次查询之间变化造成伪不变量异常；分页请求之间仍可能出现新记录，界面按 id 去重。
