# 计划审核与执行准入

本模块扩展现有 FastAPI/PostgreSQL MediaHost，不启用 apps/server 的内存执行器。

## 边界与契约

`agent/admission/policy.py` 执行三道程序检查：

1. 单子合法：严格 Schema、DAG、能力启用与绑定、读资产/写节点范围、任务展开及积分预算。
2. 类型衔接：复用现有 DAG 引用与能力检查，要求图片输入、合法依赖与引用数量。
3. 用户原意：对比 Host 冻结的模板契约，禁止更换提示词、目标类型、数量、依赖或失败策略；视频歧义必须澄清，视频不能降级成图，内容拦截不能被新提案绕过。

原意检查是保守的模板一致性检查，不声称能判定任意自然语言语义。当前可执行范围仍为现有图片/视频模板；不支持的自由计划须澄清或经产品入口建立新的用户需求。审核不会调用模型，也不把模型自检当成批准。

`IntentContract` 由 Host 创建，不接受模型或浏览器随单子一起提供。`Proposal` 支持 rule/small/llm/skill/helper/user 来源，全部进入 `propose`。现有生成路径由规则模板写单；其余来源共用准入契约，本次没有新增助手运行、技能管理或用户编辑计划的 HTTP API。大/小模型和助手来源必须引用本 Run 已完成且包含该单子的 `AgentModelCall.response`，原始输出保留在既有模型调用表；恢复只读记录。

## 数据与状态

迁移 `0014` 新增 `agent_plan_versions`、`agent_plan_reviews`、`agent_execution_admissions`。PostgreSQL 触发器禁止修改和删除这三类记录；更改提案创建新版本。Run 保存当前版本及回执引用，计划写入要求 `expected_version` CAS；持久化状态变更和只带 `runId` 的 Outbox 同事务写入，复用既有 `agent.due` 路由。重复提案复用已有审核记录，过时写入返回 `STALE_VERSION`。

审核结果为 approved/rejected/clarify。被拒绝的计划和原因保留，没有积分预占、Job 或画布占位节点。通过后创建对应版本的 AgentStep，并进入原有确认/执行流程。无付费副作用前允许在相同冻结契约内提交新版本，旧回执不能执行新版本；已有预占或执行必须创建新 Run，不能原地改写计划。重试遵守被冻结的累计积分预算。

积分预占、确认、重试、派发、生成受理和画布写入均核验准入。提交到供应商前，执行存储再次核验计划、绑定、任务哈希、积分流水、输入资产身份、资产封禁与节点租约。失败记录为 `ADMISSION_REQUIRED`，不增加供应商提交次数。数据库状态机决定完成，画布代码决定坐标。

既有租约、供应商提交登记、unknown 对账、积分结算和恢复扫描保持为事实来源。没有供应商幂等支持时，未知提交不重发。准入失效进入 `needs_attention`；取消及已有任务对账仍可运行。队列通知只是唤醒，不携带计划或输出。SSE 的 `planReview` 只包含审核结果，不公开冻结路由和模型原始输出。

## 配置与迁移

能力目录增加可选配置：

```json
"admission": {"maxTasks": 32, "maxExpandedTasks": 64, "maxCredits": 1000000}
```

这些是可调预算，不能超过现有媒体协议的硬容量。配置随 Run 冻结，修改部署配置不会扩大运行中的预算；视频、合规等硬规则没有关闭开关。

先对独立环境执行 `PYTHONPATH=apps/api AIVERO_DB_URL=... .venv/bin/alembic upgrade head`，再按正常发布流程升级 API 和 Worker。**旧活跃 Run 没有准入回执，将停止新增提交/画布交付并进入 needs_attention，不自动补发批准。** 上线前先停止接收任务，完成或取消并对账旧 Run；历史记录仍可读取。存在活跃媒体运行时，0014 降级会拒绝执行。

## 评测与验证

评测用例在 `evals/datasets/agent-admission.jsonl`，可通过现有后台导入。评测后台复用 `agent_flow`，`input.params.admission` 可携带 `catalog`、`proposal`、`intent` 的离线夹具。`expect` 支持 admissionVerdict/admissionLegal/admissionTypes/admissionIntent。实际调用同一审核函数并输出 observation.json；不调用生成接口，结果仅证明规则行为。已有 Node 对话评测路径保持原状。

```sh
.venv/bin/python -m pytest tests/backend/test_plan_admission.py -q
npm run verify
AIVERO_DURABLE_TEST_DB=... AIVERO_DURABLE_TEST_REDIS=... .venv/bin/python -m pytest tests/integration/test_plan_admission.py tests/integration/test_host_media.py tests/integration/test_production_api.py -q
```

集成测试必须显式使用独立且已迁移的 PostgreSQL/Redis；生成全部使用 Mock，不使用真实付费凭据。覆盖来源一致性、审核拒绝无副作用、模板偏离、预算、视频歧义与拦截、事务回滚、进程重建、重复投递、并发版本冲突、旧回执失效、模型输出复用、排队后篡改及不可变约束。
