# 规则主管与持久化运行链路

本块不接大模型。RulePlanner 处理明确按钮、明确单步生成和抠图；不确定视频进入文字澄清，不猜时长、不降级图片。FakePlanner 按数据库调用上下文中的 attempt 选择脚本，重建实例不会重置进度。主管只写任务单；审核使用已有 core/review 三道检查及 agent/admission/tool_types 类型门，工人继续使用共享队列、积分、MediaHost、画布接口。仍在使用的旧 Agent/手动画布流程保持可用。

## 启动与接口

先执行 Alembic 到 `0020`，按已有工具目录服务发布一个新目录版本（`build_catalog` + `tools.store.publish`），包含 detect_subject v2。发布不修改已锁定目录；已有 Run 继续按原版本运行。API/工人配置稳定的 `AIVERO_EXECUTION_TICKET_KEY`。Compose 新增 core publisher、receiver、processor、timer、recovery、supervisor 六个独立服务；单独启动主管：

```sh
PYTHONPATH=apps/api .venv/bin/python -m backend.workers.core_events supervisor
```

- `POST /api/core/runs`：document_id、request_key、message、selected_assets、mode、credits_budget、token_budget、max_thought_rounds；按钮可传 action=cutout，或 action=tool + tool + parameters。同一 request_key 同内容返回同一 Run，不同内容返回冲突。同一用户/画布 Session 同时只接受一个活动 Run。
- `GET /api/core/runs/{id}`：鉴权后的 Run/Task、文字标签、问题选项、模板消息；不返回候选蒙版、存储路径或服务商信息。
- `POST /api/core/tasks/{id}/answer`：`{"option_id":"0","request_key":"answer-1"}`。下拉选项只收 id；全部为 `all`。无选项的澄清问题可在 option_id 提交文字，保存后结束澄清，不自动重规划。相同回答键幂等；越权、过期或伪造选项被拒绝。
- 现有 `/api/events` 增加 `core_run` 事件，内容与查询投影相同，Redis 只负责叫醒，恢复/断线以数据库快照为准。前端表单与消费新事件在块 14 实现，本块不改前端。

## 持久化与审核

Run 创建时用现有 AssetReference 固定输入资产，防止等待/重启期间被历史清理删除；引用随 Run 保留，后续统一生命周期负责释放。Run 创建事务冻结画布 revision/内容、选中资产元数据、预算及能力表/工具说明/技能/资产卡版本。版本锁带 supervisor=rule-v1，仅此入口的 Run 由新主管管理；旧 Run 不被接管。

事件消费器只写 core_planner_invocations，和消费回执一起提交；不调用 Planner、不等待生成。独立主管调度器按 Run 行锁、调用顺序及租约领取调用。Planner 输入冻结后提交，调用在事务外完成，原始输出独立提交，再进行审核及 Plan/Task 创建。数据库禁止覆盖已保存输出/上下文与调用身份；已保存输出恢复时不重调。调用尚未保存就崩溃时可以重算规则/假计划；未来大模型接入必须另行提供调用幂等/对账，不能声称数据库能恢复未收到的输出。

每次有效唤醒计一次思考轮数，同一事件的审核修正不额外计轮；每份已保存结果的 token 只入账一次。超出预算不会写出违反约束的计数，完整实际 token 报告仍在调用结果中，Run 失败并说明。默认最多修正 2 次，即初审加两次重写共 3 次。每个审核结果包含三道检查及类型衔接约束；不接受 Planner 改写冻结 invariants。审核通过才创建执行准入；只排入依赖成功且已获用户确认的任务。

没有任务失败时由所有 Task 终态决定 Run 完成；失败/模型拦截事件叫醒主管，取消未执行的后续步骤，保留已成功产物。已有提交通过工人的取消/实际结算逻辑处理，不把终止主管等同于上游已免费取消。timeout 的未知付费提交仍由执行层对账，不盲目重提或退款。

## 抠图

固定 t1=detect_subject(image,target_description)，t2=cutout(image,$t1.output)。`output` 是程序选择后才可解析的 mask 端口；实际识别报告保持原始候选，不被选择结果覆盖。程序选择及所选 handle 保存在 t1.waiting，ask_user 的选项与候选索引映射保存在交互 Task。接口不能上传或指定任意蒙版。

单个候选/唯一目标标签匹配、且置信度达标时自动继续。目标匹配仅采用明确标签包含关系，不能确定则询问；多主体、低或未知置信度询问；plan 模式识别后始终询问。全部选择由工人对同源同尺寸 mask 取并集合成。无主体取消 t2，识别零费用并提示。

生产复用 Recognition/rembg 和 normalize_rembg。rembg 仅能给合并前景、置信度未知，故真实路径会询问；多实例标签/置信度的行为用假识别接口验证，不能承诺当前 rembg 支持杯子实例分割。单进程共用识别线程，取消不会提前释放仍运行的原生推理槽位。

Agent 候选是当前任务执行中间文件，按不可猜测 handle 绑定来源 hash/尺寸，保存在共享媒体目录，支持服务重启和等待用户；不注册为公开资产，不用于缓存或恢复手动画布蒙版/笔画。再次发起识别使用新任务，不复用旧选区。中间文件目前随执行数据保留；终态清理/保留周期由后续统一生命周期模块实现。

计划占位节点由执行层按代码布局写入，必须存在审核准入；产物发布时以稳定 Task 节点 id 替换占位。失败/取消时清理占位；原图保留。原有锚点 CAS、占用和最近空位规则继续生效。

## 配置与回归

`AIVERO_SUPERVISOR_`：review_retries、confidence_threshold、lease_seconds、planner_timeout_seconds、concurrency、scan_limit、context_messages、max_tasks、lock_timeout_ms、retry_seconds、planner_failures；基础预算沿用 `AIVERO_CORE_`，执行层沿用 `AIVERO_EXECUTION_`。

```sh
PYTHONPATH=apps/api .venv/bin/pytest -q tests/backend/test_supervisor.py tests/backend/test_core_review.py
AIVERO_SUPERVISOR_TEST_DB=... PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_supervisor_pipeline.py
AIVERO_EXECUTION_TEST_DB=... AIVERO_DURABLE_TEST_DB=... AIVERO_DURABLE_TEST_REDIS=... PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_execution_worker.py tests/integration/test_production_api.py tests/integration/test_core_contracts.py
AIVERO_CORE_MIGRATION_TEST_DB=... PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_core_migrations.py
npm run verify
```

所有 DB 都必须是显式指定的隔离测试库。覆盖两步图→视频、审核三次失败、失败保留首步、单/多/无主体、目标与全部选择、计划确认/取消、回答鉴权/幂等、占位替换，以及事件唤醒/原始输出/计划提交/两步产物提交边界的真实 SIGKILL。执行层另有服务商接受后、ID 落库前 SIGKILL 回归。没有真实大模型或付费生成调用。

本次交付使用独立暂存快照验证：`npm run verify` 通过（后端 984 passed、2 skipped）；主管完整集成 19 passed；旧执行层/生产 API/Core 契约 99 passed；空库升级、回滚、重升 1 passed。Compose 配置解析通过。预置审核模块单独提交 `ca4f12a`，主管链路另行提交；未纳入工作区中的前端与其他未提交文件。
