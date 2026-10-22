# Core Task 执行层

执行层不调用大模型、不扩写提示词、不重规划。AI 仍只能写任务单。工人复用原 `Job → outbox → RabbitMQ → aivideo.execute_phase → generation.image/video、exports` 队列，`coreTaskId` 将现有 Celery 入口分派到 Core 工人；普通生成和导出继续走原入口。API 回调只接收收据、更新数据库及唤醒队列，不持有服务商生成凭据。

## 接线与准入

迁移到 `0019`。部署为 API 与工人配置相同且稳定的 `AIVERO_EXECUTION_TICKET_KEY`（Fernet key）；回调另外配置 `AIVERO_EXECUTION_CALLBACK_SECRET`。密钥没有默认值，不进入 Run、工具定义、数据库或消息。生成凭据仍由工人通过现有 `AIVERO_CREDENTIALS_FILE` 和 credentialRef 解析。Compose 已透传执行配置；旧生成任务不要求执行层密钥。

审核程序在 Task=validated 时调用 `record_admission(tx, task, catalog, checks=..., review_ref=..., placement=...)`，然后通过原状态机转到 queued。该函数不是 HTTP 或模型工具，不运行或替代三道审核：它仅封存审核程序给出的三道通过凭证、计划、参数/范围哈希、工具目录哈希及代码布局指令。工人不签发自己的审核结果。调用方必须是真正完成三道检查的程序，不能把模型自报的布尔值传给它。

可直接调用事务内 `enqueue(tx, task_id)`；现有调度 daemon 也会扫描有准入凭证、尚未关联 Job 的 queued Task。没有凭证、参数或版本变化的单子不会花钱。同一 Task 的 Job 和执行记录唯一；依赖不是全部 succeeded 时不提交。输入引用支持 `$tN.output` 和审核器已经支持的候选 mask 路径；读取实际资产时检查工作区、可读范围和类型。

生成批量必须在审核前展开成逐项任务（staged 执行要求 count=1）；工人不会私自扩展计划。读取/表单/等待任务继续由其已有处理器负责，本执行管线负责有产物的 call_tool。临时蒙版不能变成持久缓存或由模型传入任意文件路径；本地抠图适配器只能消费宿主已绑定的、仍可读取的蒙版输入。失效输入失败后交主管处理，不恢复旧选区或偷偷重新选择蒙版。

## 检查点与恢复

数据库记录执行阶段：ready → preparing（如需要上传/准备工作流）→ sending → poll → download → publish → done；unknown 表示提交/费用仍需对账。Task 的业务状态只通过原 `core_transition` 改变。TaskExecution 使用短事务、非阻塞行锁、租约 token 和执行轮次；网络和文件验证不持有数据库锁。一个 Run/文档忙时跳过，不在批量循环里持锁等待。

1. 锁 Run、Task 和共享积分账户，在 Run 上限内预扣；使用原 `host_credit_accounts/host_credit_ledger`，没有新增积分账户体系。新流水关联 Core Run/Task，旧流水保留旧 Agent Run/Step；两种所有权互斥。
2. 服务商准备与付费提交分开落库。`CoreTaskAttempt` 在发出提交前记录参数哈希、幂等键和发送确定性。超时和进程终止不等于请求未发出。
3. 已有登记时优先按服务商 ID 或登记查询。只有锁定定义和真实适配器都支持幂等，才允许携带同一幂等键恢复提交。没有查询能力、也没有幂等能力时保留 unknown、冻结预扣并告警，绝不假装没提交然后退款重提。
4. 回调与轮询进入同一收据处理函数，以服务商绑定作用域 + provider_task_id 去重；没有第三方任务 ID 的同步响应使用内部收据关联键，不伪造第三方任务 ID。回调先验签和核对任务绑定；早到但尚不能确认绑定的回调返回可重试状态。终态重复回调正常确认，不再次扣费或写画布。
5. 得到定位信息后，先加密保存带过期时间的私有下载票据，再立即转存；正常工人不需要等待下一轮轮询才开始下载。资产表、画布、公开结果只保存自有资产信息。工人复用现有公网下载客户端的 DNS/IP 检查、来源白名单、无重定向规则，不向产物站点转发服务商凭据。
6. 文件复用 `ArtifactSpool` 的原子写和收据。每次执行写不同轮次目录，旧工人的迟到结果不能覆盖已发布文件；下载后、写数据库前崩溃可从收据恢复。失败下载在票据有效期内退避重试，过期后不重新生成。
7. 资产（含 Core 版本与产生它的 Run/Task/user）、文档写入凭证、实际积分结算、Task 终态及结果 outbox 在同一事务完成。失败回滚不留下半次画布发布。完成后清除执行和回调收据中的下载票据。

内容拦截不重试，退还本任务积分，Task=blocked_by_model，发 task_blocked；明确 retryable 的生成失败按 Task.max_retries 重试。其他明确失败退款并发 task_failed。提交或查询结果未知不能推断已经免费失败，因此保留对账状态，不自动重提或结算。实际花费超出审核预扣时冻结并告警，工人不自行突破预算。

## 画布、取消和费用

范围键采用 `tools`（工具名或 name@version）、`readable_assets`、`writable_regions`。仅支持当前字段契约，`readable_asset_ids` 等旧字段不会授予访问权限。命名区域的几何边界从冻结 snapshot.regions/objects 读取，不能由模型传坐标；显式全画布授权可用 `write_canvas=true`，也可附 `writable_region` 收紧几何范围。空白范围不会默认放行工具或画布写入。

位置只由 `layout.py` 根据相对描述计算。替换检查锚点 assetVersion/busy 标记；文档行锁与持久写入凭证使区域检查和落点在同一事务。冲突时在配置的网格范围内查找最近空位，并发 conflict；落点不得超出 Run 的可写区域。找不到合法空位则仅保留产物。一个 Task 只产生一份 CanvasWrite 凭证，重复消息不会重复发布。

确认服务商取消终态后才退款；“取消接口已受理”不是终止证明。不可取消或已经完成的任务继续转存，产物进入现有生成历史，不写画布，按实际结算。取消是数据库标志，不依靠杀工人或 revoke 代表上游取消。

- `GET /api/core/runs/{run_id}/costs`：复用现有登录鉴权；限制工作区和 Run 所属用户。逐步骤返回预扣、实际花费、退款、重试次数及结算状态。
- `POST /api/core/tasks/{task_id}/cancel`：同样检查归属，只登记取消请求；网络取消仍由工人执行。
- `POST /api/core/provider-callbacks/{job_id}`：请求头 `x-provider-signature` 为 HMAC-SHA256，内容是 `str(job_id) + "\n" + 原始请求体`。Body 含 `provider_ref` 和序列化 ProviderResult；不接受媒体二进制。签名包含路径绑定，不能移用到另一 Job。

`before_call(task)`、`after_result(task, asset)` 默认均为空。前者若改写参数，必须重新取得审核准入，不能沿用旧凭证；后者可返回 `[{"type": "asset_changed", "ids": {"asset_id": "..."}}]`，由工人在同一事务写 outbox，不直接向外发送消息。

## 配置与限制

`WorkerSettings` / `AIVERO_EXECUTION_` 提供租约、调用超时、轮询、退避及抖动、对账间隔、扫描数量、锁等待上限、布局间距/搜索数量、输出画幅、视频探测和回调大小限制。排队 TTL 复用 ProviderPolicy.queue_s；生成时限来自冻结工具 SLA。网络或本地处理期间由独立心跳续租；心跳间隔不会超过租约的三分之一，崩溃后无需等待整个长调用超时才恢复。耗时较长的导出按实际资源配置调用超时。

现有真实服务商大多没有按登记查询的能力，也没有提交幂等承诺，适配器不会声称支持。它们的未知提交不能自动保证完成，但可以保证不盲目重复提交。服务在链接过期之后才恢复且没有可重取链接的接口时，也无法凭空找回未转存的字节；该情况明确失败/对账，不重复生成。真正服务商取消支持以实际接口为准，本地原生计算不会被虚假标成已取消。

## 可复用假服务商与验证

`FakeProvider(path, Scenario(...))` 使用独立 SQLite 文件作为假服务商自己的任务库，工人进程退出后记录仍在；`create_fake_app` 可单独启动 HTTP 服务供后续块复用。可配置成功、接受后丢响应、可重试失败、内容拦截、回调份数（0=丢失）、结果 TTL、延迟完成、下载故障、不可取消与不支持幂等。它不访问真实付费凭据。部署中的故障注入 adapter 只允许显式 development 环境。

```sh
PYTHONPATH=apps/api .venv/bin/pytest -q tests/backend/test_execution_contracts.py tests/backend/test_execution_fake.py
AIVERO_EXECUTION_TEST_DB=... PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_execution_worker.py
AIVERO_DURABLE_TEST_DB=... AIVERO_DURABLE_TEST_REDIS=... PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_production_api.py tests/integration/test_core_contracts.py
AIVERO_CORE_MIGRATION_TEST_DB=... PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_core_migrations.py
npm run verify
```

全部生成验证使用假服务商或 HTTP MockTransport。集成套件包含真实 spawned 进程的 SIGKILL（外部已接受、provider_task_id 尚未落库），重启后断言只有一次提交和一次结算；并覆盖 3 次回调、丢回调、60 秒票据提前转存、拦截退款、不可取消历史、跨 Run 区域竞争、准备阶段重试、签名与归属、事务回滚、非幂等且不可查询时拒绝重提。没有真实服务商生成冒烟。

### 本次交付验证记录

- 独立暂存快照执行 `npm run verify`：通过；后端 922 passed、2 skipped（显式环境集成测试另行运行），前端及 creation-server 校验通过。
- 本块纯单测与隔离 PostgreSQL 集成测试：26 passed；含画布锁竞争期间保存服务商回执、视频保持视频类型、签名回调格式拒绝。
- 原有生产 API 与 Core 契约集成：80 passed；空库全迁移、回滚、重新升级：1 passed。全部使用独立临时 PostgreSQL/Redis，不连接生产数据。
- 第一批提交 `9ceb4b4` 提供持久化假服务商；第二批接入执行层及上述回归。真实服务商只验证适配错误与协议，不运行付费生成。
