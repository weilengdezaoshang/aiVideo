# 创作 Host 服务端：S1 / S2 / S3

**S1 ports 已对齐；S3 interrupt + handoff + todo_plan 已实现。**

这是 aiVideo 中独立的 Node 22 / TypeScript / Hono / zod 对话联调服务，不替换现有 PostgreSQL/FastAPI 应用。无 LLM Key，生成与积分均为 mock。Ports 和 Trace v1 以用户提供的 PORTS_AND_TRACE.md 为准。

## 启动与联调

在仓库根安装依赖并启动（Node 22）：

```sh
npm install
npm run dev:creation-server
# 或 cd apps/server && npm run dev
```

默认 `127.0.0.1:8787`。可配置：

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| PORT | 8787 | 监听端口 |
| PUBLIC_BASE_URL | http://localhost:8787 | 作品 URL 前缀 |
| CORS_ORIGINS | localhost:5173 / 127.0.0.1:5173 对应 http Origin | 逗号分隔精确 Origin |
| TRACE_DIR | 仓库根 eval/runs | Trace 输出目录 |
| INTERRUPT_CREDITS_THRESHOLD | 50 | 未批准的剩余生成预估 **大于** 此值时中断 |
| MOCK_IMAGE_CREDITS | 40 | 每张 mock 图的预估与结果积分 |

现有前端：

```sh
cd apps/web/creation-agent
npm install
npm run dev
```

前端「服务端 Host」默认指向 `http://127.0.0.1:8787`。普通输入「生成一张咖啡海报」直接出图；输入「做一条30秒咖啡广告，3个镜头」走真实 SSE 计划与 120 积分确认。两个 P0/P1 演示按钮仍是本地 mock，不能代替服务端联调。

```sh
curl -i http://127.0.0.1:8787/health
curl -N -X POST http://127.0.0.1:8787/v1/conversation \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"生成一张咖啡海报"}'
curl -N -X POST http://127.0.0.1:8787/v1/conversation \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"做一条咖啡广告，3个镜头"}'
```

在 interrupt_approval 中取得 conversation_id、confirmId，再开新的 POST SSE：

```sh
curl -N -X POST http://127.0.0.1:8787/v1/conversation \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":"替换为UUID","action":"resume_interrupt","confirmId":"替换为确认ID","approved":true}'
# 取消：approved 改为 false
```

stop_stream 可停止执行或取消仍在等待的中断：

```sh
curl -X POST http://127.0.0.1:8787/v1/stop_stream \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":"替换为UUID","turn_uuid":"替换为UUID"}'
```

## 分层与 ports

```text
app/main → app/create-app（唯一 composition root）
                 ├─ api/routes：HTTP 校验 / CORS / SSE，无业务编排
                 └─ domain/ConversationService：受理 / snapshot 恢复 / Trace
                              ↓
                       agent/AgentLoop：Host ↔ Planner 串行阶段执行
                              ↓
                       ports：SessionStore / EventLog / TaskQueue /
                              RateLimiter / LlmProvider / TraceWriter / Tool
                              ↑
                       adapters/memory、tools/text2image、trace/file-writer
```

- SessionStore：conversation CRUD、turn 读写、get/save/clearInterruptSnapshot；S3 增加原子 claimInterrupt、activateTurn 和 settleInterrupt。确认的决定与认领回执保存在 TurnRecord，不散落全局变量。
- EventLog：conversation 级 seq，append、readFrom（不含给定 seq）、getLastSeq；listByTurn 仅是 convenience。事件先 append 再写 SSE。
- TaskQueue：enqueue/dequeue/complete/fail/get/getByIdempotencyKey；Memory 同步消费 handler。生图入口始终 enqueue，idempotencyKey=tool_call_id；旧 execute 只作 deprecated 薄封装。
- RateLimiter.allow 返回 `{ok,retryAfterMs?}`；当前是明确的放行桩，不是生产限流。
- LlmProvider.act 使用 host/planner、messages、tools；规则 stub，不调用真实模型。
- TraceWriter.write 接受 AgentTraceV1 并返回 `{path}`。Memory 适配器用于测试，默认使用文件适配器。

Memory → Redis 后续只替换适配器：SessionStore 用事务/Lua 原子维护轮次、snapshot 与确认回执；EventLog 用 Streams；TaskQueue 换 BullMQ 并通过协调通道传取消信号；RateLimiter 换原子限流。在 app 注入新实现，不让 api/domain/agent 导入 Redis。复用 ports 测试，并在 S4/S5 补跨进程取消和恢复测试。此阶段没有 Redis/BullMQ、队列集群或真实限流。

## S3 主流程与事件契约

短 prompt：Host 文本 → enqueue text2image → tool_status / artifact → complete。默认预估 40 ≤ 50，无 handoff 或中断。

复杂 prompt 匹配广告、镜头、套图、分镜或明确多段视频：Host 短文本 → handoff_to_planner → Planner 三个镜头计划 → 更新第一步 in_progress → 预估剩余三图 120 > 50 → 中断。批准后执行三个生成任务、逐步完成计划，再 handoff_to_host 输出总结。handoff 是串行控制权转交，不是并行子 agent 或客户端 MCP 实现。

中断前创建 pending confirm part，保存 snapshot 的 pendingToolCall、activeAgent、cursor.lastEventSeq 和可序列化 continuation。continuation 包括阶段、剩余调用、计划、批准范围与 Trace 累计信息。当前 SSE 正常结束，不发 stream_complete，不挂住连接；turn=interrupted，Trace outcome=interrupted。

恢复只从 SessionStore 的 snapshot 读取续执行数据，保持原 turn、runId、assistant message ID。批准 clear snapshot，confirm=accepted，执行待处理调用；取消 clear snapshot，confirm=rejected，计划的执行中步骤取消，无新图。默认未执行路径文案为「已取消，未消耗积分。」；若此前已完成作品，则明确保留作品并不再新增消耗。

同一 confirmId：原子认领后第二个并发请求返回 409；处理完成后相同决定返回当前消息快照，不重跑工具；相反决定返回 409；不存在/已停止的确认返回 404。一个 turn 可因工具 need_user 再次中断，生成新的 confirmId。

工具返回 need_user 时不追加 artifact，保存新的续工具调用。可返回 pendingToolCall 指定续调用；否则生成新调用 ID 并携带 approved=true。新 ID 区分“询问任务”和“已批准的生成任务”，各自仍遵守 enqueue 幂等。

POST 输入：

- 新轮次：`{prompt,conversation_id?}`。
- 恢复：`{conversation_id,action:"resume_interrupt",confirmId,approved}`。
- 校验错误 400，未知确认 404，确认冲突 409，限流拒绝 429。

SSE event 仍为 system / text_delta / message；data JSON 统一含 conversation_id、turn_uuid、runId、sequence、timestamp。sequence 在 conversation 内严格递增，SSE id=`runId:sequence`。

| system.type | 特有数据 | UI |
| --- | --- | --- |
| stream_start | 无 | streaming |
| todo_plan | explanation、steps（id/text/status） | 同一 assistant 的唯一 plan 原地更新 |
| interrupt_approval | confirmId、title、estimateCredits、summary | awaiting_user，滚到卡片并聚焦 |
| stream_complete | status=completed/cancelled | idle，恢复输入 |
| stream_error | message、status | 显示失败并恢复输入 |

message 是 assistant 快照，parts 支持 text / tool_status / artifact / plan / confirm / error。confirm 在恢复快照中 accepted/rejected；计划卡不重复插入。前端快照替换 parts，delta 追加唯一 text；恢复使用新 HTTP SSE，继续相同消息。HTTP 请求失败且尚未恢复时保持 pending，可再点；恢复流已启动后异常则报告失败，避免假装积分未消耗。

正常中断关闭 SSE 不会取消 snapshot。真正执行中的 HTTP 断开触发取消；stop_stream 可显式取消中断。迟到事件不覆盖 cancelled，完成作品保留。

## Trace v1、迁移与 Memory 限制

输出 `eval/runs/<runId>.json`，首次中断与后续恢复写入同一 run 文件的最新状态。version="1"、conversationId、startedAt/finishedAt、turns 均符合冻结 schema；每个 turn 记录 agents（host/planner steps）、events、toolCalls（handoff_* / gen_*、jobId、credits、状态）、interrupts（reason、resumed）、outcome 与 metrics。中断时为 interrupted，批准完成时 success，取消时 cancelled。每次写入通过 zod 校验、临时文件与 rename；写入失败发 stream_error，不能报告完成。

Trace.events 是截至终态 message 的日志摘要，不含其后用于关闭流的终态 system；interrupts 单独记录确认历史。积分 metrics 按每次已成功工具结果累计，未执行/询问任务不计消耗。

旧文件仍由显式工具迁移，启动不自动改写：

```sh
npm run migrate:trace-v1 --workspace @frayune/creation-server -- --dry-run
npm run migrate:trace-v1 --workspace @frayune/creation-server
```

Memory 数据仅存进程内：**重启进程会丢失 conversation/snapshot/确认回执/job 数据**。接口和 snapshot 完备；测试验证只拷贝 conversation、TurnRecord（含 snapshot）到新的 MemorySessionStore，重新组装 Host 仍能恢复。生产重启恢复需要持久化适配器，留给后续阶段，不承诺当前 Memory 可跨进程保留。

不修改 eval/cases。未实现 S4 用户并发/生产限流/Redis，未实现 S5 真实评测门禁，未实现 S6 画布、额外子 agent、审核。

## 检查与 S3 验收

```sh
npm test --workspace @frayune/creation-server
npm run typecheck --workspace @frayune/creation-server
npm run verify
```

S1 ports 契约、迁移测试继续保留；S3 测试涵盖批准/取消、重复与并发、snapshot 重建、阈值边界、need_user 与等待中停止。前端真实 SSE→store 测试覆盖同消息/唯一计划、恢复失败重试和同轮多次中断。

2026-10-09 实测结果：

| S3 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 短 prompt 出图回归 | Pass | HTTP SSE：start → 文本/工具/单图 → complete |
| 高消耗/复杂请求先中断，无成功 artifact | Pass | 复杂请求暂停在 120 积分，SSE 正常关闭 |
| 批准后工具、作品与完成 | Pass | HTTP 与浏览器确认后，同条助手消息生成三张图 |
| 取消后文案、无新图、可再次输入 | Pass | HTTP 与浏览器取消通过；计划显示已取消，焦点回输入框 |
| snapshot 经 SessionStore 恢复 | Pass | 复制 snapshot 重建 Memory 适配器的测试通过；跨进程持久化尚不支持 |
| handoff 与 todo_plan 原地更新 | Pass | Host → Planner → Host，单一计划卡逐步完成 |
| Trace v1 interrupts / agents | Pass | schema 校验与 Trace 内容测试通过，取消 resumed=false |
| 幂等确认、失败重试与停止隔离 | Pass | 重复/并发确认、二次中断、取消激活竞态测试通过 |
| npm run verify | Pass | 前端 368 项通过；后端 486 项通过、2 项跳过，现有依赖弃用警告 1 项 |
| Node 22 / 独立前端构建 | Pass | Node 22 服务端 19 项通过；Vite build 通过 |
| 未修改 eval/cases、未实现 S4/S5/S6 | Pass | 改动限定于创作服务、对话接线、测试与文档 |

S1/S2 完成；S3 interrupt + handoff 已完成。后续持久化与真实 Provider 不属于本次范围。


## 取消后继续

确认卡取消时，通过 SessionStore.saveContinuationDraft 在 conversation 保存原需求、执行状态、工具 ID、游标和计划。interrupt snapshot 仍按原协议清除；明确取消确认卡，以及有计划的运行在停止时会留下 draft；失败不会创建 draft。

普通发送「继续」「接着做」「接着」「继续执行」「继续生成」「继续上次」或「resume」会在该会话有 draft 时续跑。规则忽略两侧空白、不区分 resume 大小写，允许结尾一个「。」「.」「!」「！」，长度不超过 20 字。混合新需求或其他句子不匹配；明确新任务清除 draft 并走原出图流程，无 draft 时仍按普通需求处理。

续跑新建 turnId/runId，工具使用原任务文案，保留执行 phase、index、工具 ID 和已完成步骤；先发布计划快照，再从未完成步骤继续。旧轮已完成作品保留在历史消息中。剩余任务重新计算积分，超阈值会出现新确认卡，回复「继续」不代替积分批准。draft 在新轮成功准备并保存状态后消费，再次取消重新保存。Memory draft 同样不跨进程持久化。

此功能是无 confirmId 的跨轮续跑；resume_interrupt 是有 confirmId 的同轮确认恢复。新 run 的 Trace 只计本轮工具与积分，原 run Trace 保留原始历史。

| 验收项 | 结果 |
| --- | --- |
| 取消后经 SessionStore 读取 draft | Pass：单测 |
| 普通发送继续重新展示同一计划与新确认卡 | Pass：服务端与真实 SSE→前端 store 测试 |
| 保留已完成步骤，从未完成 index 续跑 | Pass：index=1 夹具验证仅执行后两步，计费 80 |
| 明确新需求清 draft、无 draft 不报错 | Pass：单测 |
| 原确认批准和短 prompt 出图回归 | Pass：原 S2/S3 测试 |
| 规则意图识别，无复杂模型、未改 eval/cases | Pass |

本次实测：浏览器「复杂广告 → 取消 → 普通发送继续 → 原计划 + 新确认卡 → 确认 → 三张图 + 总结」Pass。`npm run verify` Pass（前端 369 项，后端 486 项通过、2 项跳过、1 项现有依赖弃用警告）；Node 22 服务端 22 项 Pass，真实 SSE→store 4 项 Pass。旧的取消记录保持取消状态，续跑在新的助手消息展示。

停止确认卡后的续跑：Pass（HTTP stop→继续计划回归）；本地 P1 演示的取消/停止→继续：Pass（mock/store 集成测试）。演示按钮会同步切换对话来源至本地演示，后续输入保持同一来源；服务端下一次发送会等待停止请求完成，防止草稿保存竞态。

## S4：限流、异步队列与容量底线

默认 CLI 使用 `QUEUE_DRIVER=memory` 的 MemoryAsyncQueue（setImmediate 调度，同进程消费）；createApplication 仍允许同步 MemoryTaskQueue 或显式注入 TaskQueue。enqueue 的 Promise 等待终态，与现有 Host 契约兼容。并发重复 key 等待同一任务，不返回幽灵 running。已取消/失败的 key 不自动重试；新任务需新 tool_call_id。

配置（均为正整数）：

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| RATE_LIMIT_DRIVER | memory | 固定窗口限流适配器 |
| QUEUE_DRIVER | memory | 可运行的本地异步队列 |
| RATE_LIMIT_REQUESTS | 60 | 每 key 每窗口请求数，含确认恢复 |
| RATE_LIMIT_WINDOW_MS | 60000 | 固定窗口长度 |
| MAX_RUNNING_TURNS | 2 | 每 key 同时执行轮数 |
| JOB_TIMEOUT_MS | 30000 | 单任务超时 |
| QUEUE_MAX_PENDING | 32 | 全队列排队及实际执行任务总容量 |

key 是 conversation_id；无 ID 的请求共享 anonymous 桶与并发槽。没有认证，因此这不是可靠的用户身份配额：客户端可换会话 ID；全局队列容量提供第二道保护。429 返回 retryAfterMs 和 Retry-After。创建/恢复入场时原子计数，完成/失败/取消/中断后释放，等待确认不占 running 槽。

任务取消、超时会 abort handler 的内部 signal，分别记录 cancelled/failed。迟到成功不能覆盖终态。无法强杀不响应 signal 的 JavaScript/native handler；其实际完成前仍占队列容量，避免超时后无限放入新任务。超过容量立即拒绝 enqueue，Host 写 failed 快照与 stream_error，不出图。超时/取消后原 enqueue 等待者收敛；日志记录 queued 与 completed/failed/cancelled，以及限流触发。

### Redis/BullMQ 替换步骤与明确限制

本次选择可测 Memory 强化 + 异步消费方案；`src/adapters/redis/contracts.ts` 是**抽象扩展骨架**，不是已实现的 Redis/BullMQ 驱动。`QUEUE_DRIVER=bullmq` 或 `RATE_LIMIT_DRIVER=redis` 当前会启动失败，禁止静默降级；Memory 限流、队列、幂等表仍会随重启丢失。

1. 本地启动 Redis：`docker run --rm --name creation-redis -p 127.0.0.1:6379:6379 redis:7-alpine`。默认开发和测试不依赖它。
2. 在 apps/server 添加 bullmq/ioredis，实现在上述骨架派生的适配器。连接只在 app 创建并注入 Ports，api/domain/agent 不接触 Redis。
3. BullMQ 使用 tool_call_id 的安全编码/哈希作为 jobId，保留 completed/failed/cancelled 收据；enqueue 用 QueueEvents 等待终态，保持当前返回契约。重试与重启不能重复收费。
4. register 映射 Worker handler；停止信号通过共享取消标志与 Worker 轮询传递，超时/迟到结果用原子状态比较保护。另定义关闭 Worker/QueueEvents 的生命周期。
5. Redis limiter 用 Lua 原子固定窗口及租约计数；release 使用唯一租约 ID，配置 TTL 与续约防进程崩溃占槽，不能直接复制当前进程内计数。
6. app 的 driver 分支实例化正式适配器；增加真实 Redis 集成测试再启用。SessionStore/EventLog 持久化仍是独立替换，不假定仅换队列就能跨重启续跑。

### S4 验收

| 项目 | 结果 |
| --- | --- |
| 可配置的非 always-allow 限流，HTTP 429 | Pass：固定窗口、cost、窗口重置、不同 key 与 Retry-After 测试 |
| 每 conversation running 上限 | Pass：入场并发拒绝、终态释放测试 |
| TaskQueue 可选同步/异步 Memory，默认 Memory 可跑 | Pass；CLI 默认异步消费，composition root 可注入 |
| BullMQ/Redis 正式驱动 | Fail / 尚未实现：仅抽象骨架与本地接入步骤，选择时明确报错 |
| 同 key 并发不双跑 | Pass：同步旧回归与异步重复等待测试 |
| stop 取消或收敛任务 | Pass：in-flight abort、终态保护与不合作 handler 容量保留测试 |
| 超时与背压 | Pass：超时 failed、满队列拒绝测试 |
| S2/S3/继续回归 | Pass：现有服务端用例保留 |
| 未实现 S5/S6，未改 eval/cases | Pass |

2026-10-10 实测：Node 22 服务端 25 项 Pass；实际 HTTP `/health` 200、异步生图 SSE 完成、同会话超限 429 + Retry-After 均 Pass。默认 8787 已以 S4 Memory 配置启动。仅修改 apps/server，未改 eval/cases 或无关脏文件。

完整 `npm run verify`：Pass（前端 370 项通过，后端 486 项通过、2 项跳过、1 项现有依赖弃用警告）；typecheck、lint 与根前端构建通过。

## Review 修复：停止续跑、消费归属与记录回收

- 续跑检查未完成工具的队列收据：cancelled/failed 使用新的执行 ID，计划步骤 ID 不变，已完成步骤不重做。生成中停止→继续→重新确认→完成的 HTTP 回归已覆盖。
- Memory enqueue 已保留自动消费归属，dequeue 不会领取这些任务，防止外部消费者与内置消费者争抢；enqueue 仍等待自动消费终态。这个实现不支持把自动消费任务转移给外部 Worker；正式 BullMQ 适配器必须自行实现领取与终态等待。
- 限流窗口在访问时清理全部过期 key；默认最多 10000 个窗口（RATE_LIMIT_MAX_KEYS），满时拒绝新增 key。
- 队列终态收据默认保留 24 小时（QUEUE_RETENTION_MS=86400000），默认最多 10000 条（QUEUE_MAX_RECORDS）。访问时惰性回收；活动任务和实际未结束的 handler 不回收。容量满时拒绝新任务，保留尚未过期的幂等保护。超过保留期后同一 key 可作为新任务执行；长期幂等需要持久化收据，不能依赖 Memory TTL。

修复验收（2026-10-10）：生成中停止后继续、自动消费/dequeue 竞争、窗口与收据过期清理三项均 Pass。Node 22 服务端 28 项 Pass；完整 npm run verify Pass（前端 370 项，后端 486 项通过、2 项跳过、1 项现有依赖警告）。
