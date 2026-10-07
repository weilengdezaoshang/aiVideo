# 画布只读 Agent

画布侧边栏的「画布助手」运行独立于文档保存和媒体生成任务。模型可以调用四个只读工具，工具返回实际观察结果；系统不接受模型自填的通过标记。首期支持 DeepSeek 和百炼的 Chat Completions 工具调用，不生成图片、不做图片语义检查、不写画布。

```mermaid
flowchart LR
  UI[画布对话面板] -->|保存确认 revision / requestId| API[Agent API]
  API -->|同事务| PG[(Session / Run / 快照 / Event / Outbox)]
  PG --> Dispatch[Outbox dispatcher]
  Dispatch --> MQ[RabbitMQ agent.due]
  MQ --> Bridge[agent-bridge]
  Bridge --> Worker[Celery agent 队列]
  Worker --> Runtime[AgentService 单阶段执行]
  Runtime -->|先保存 sending| PG
  Runtime --> Model[DeepSeek / 百炼]
  Model -->|工具调用提议| Runtime
  Runtime --> Tools[只读工具执行器]
  Tools -->|快照 / 文件尺寸和哈希| Runtime
  Runtime -->|结果 + 下一阶段 + Event + Outbox| PG
  PG -->|持久事件 SSE 重放| UI
```

## 配置和启动

1. 执行 `alembic upgrade head`，本功能新增迁移至 `0012`（Agent 阶段表：`agent_steps`、`agent_reflections`，Run 增加 `stage`/`plan`/`waiting`）。
2. 复制 `deploy/agent-routes.example.json` 到部署配置目录，填入供应商实际可用的模型名、区域/工作区 `baseUrl`、路由标签及 `credentialRef`。地址为 Chat Completions 的基础地址，不含 `/chat/completions`。
3. API 设置 `AIVERO_PRODUCTION_AGENT_ROUTES_FILE`；Compose 使用宿主机 `AIVERO_AGENT_ROUTES_FILE` 映射它。未配置时加载空列表，面板显示暂无可用模型。
4. 可选：复制 `deploy/agent-personas.example.json` 并修改，API 设置 `AIVERO_PRODUCTION_AGENT_PERSONAS_FILE`（Compose 使用宿主机 `AIVERO_AGENT_PERSONAS_FILE`）。人格只描述身份、语气、工作信条和表达规范，不能包含工具名或 `[E1]` 这类控制标记，也不授予任何权限；格式错误或路由引用了不存在的人格时 API 拒绝启动。在路由中用 `"personaId"` 选择人格，未配置时使用内置的“画布助手”。人格在创建 Run 时冻结，修改配置只影响之后的新 Run。
4. Worker 的 `AIVERO_CREDENTIALS_FILE` 与现有生成 Worker 共用凭据注册表，新增所引用的条目，例如 `{"deepseek":{"apiKey":"实际密钥"},"bailian":{"apiKey":"实际密钥"}}`。密钥不放路由文件、不返回浏览器。
5. `npm run stack:up` 会启动 `agent-worker` 和 `agent-bridge`；现有 scheduler 同时处理 Agent 恢复，dispatcher 负责发布 Agent Outbox。Agent worker 只消费 `agent` 队列，媒体 Job 契约保持原样。

运行开始后固定模型路由。API 展示的是已配置路由，实际凭据是否有效由 Worker 判定；不会偷偷降级到 Mock。默认选择 DeepSeek，模型能力和地区地址应按各供应商当前官方文档配置。

可选路由字段：`timeoutSeconds` 默认 60（1–180），`maxTokens` 默认 2048（128–8192），`contextMaxBytes` 默认 131072，`toolOutputMaxBytes` 默认 16384。DeepSeek 显式发送 `thinking.type=disabled`，百炼发送 `enable_thinking=false`；不使用文本逐字流。

## 运行与证据

- 每条消息创建一个 Run，每文档仅允许一个 queued/running Run。模型最多 8 次、工具最多 12 次，失败也计数；超额工具批次整批拒绝。
- `read_canvas` / `list_canvas_elements` / `get_canvas_elements` 只读发送时确认的版本与选区。当前生成实体会被投影为画布上可见的已完成媒体。临时蒙版、笔画、识别记录和旧聊天不进入 Agent 快照。
- `inspect_image` 只接受本 Run 绑定的素材 ID，读取共享目录真实文件、验证冻结的大小和哈希，再测量尺寸和格式。图片最多 25 MiB、4000 万像素。画布上缩放后的宽高不是图片实际像素。
- Session 事件序号在同一 Session 行锁内分配和提交；快照及覆盖游标也在该锁内读取。SSE 轮询已持久化事件，最多约 2 秒刷新，无 token 流，也不借重连执行模型。
- 对话、调用记录和待确认 requestId 使用独立 Store。发送前只保存未保存的编辑，干净文档不会 POST。请求响应丢失时按原 ID 查询或显式重发原请求，不生成新 ID。
- 模型已发送但结果未知时变为 `needs_attention`，不自动再发。停止会尽力取消在途 HTTP 的本地等待，不能证明供应商已停止计费；迟到结果只归档。
- 删除画布会同时关闭会话和停止活动 Run；来源 ID、调用记录和素材引用仍保留。本期没有这些记录的自动清理任务。

## 阶段化运行与等待状态

- Run 按 `plan → execute → respond → reflect` 阶段推进，阶段与当前步骤记录在 Run 的 `stage` / `plan` 投影中；面板的「执行计划」卡片显示每步状态（待执行/执行中/已完成/受阻/已废弃），重新规划时旧步骤标为已废弃并升版本号。
- 模型需要澄清时调用 `ask_user`，Run 进入 `waiting_input` 并带 `waiting.question`；用户发送下一条消息即视为回答，上一条待答问题会进入新 Run 的上下文。
- 生成类 effect 工具本期未注册（启动时断言生产注册表不含 effect 工具）。预留的 `waiting_approval` 状态只在未来接入生成链路时出现；届时 `ApprovalEvidence` 只能由 Runtime 依据用户动作写入，模型无法通过工具调用伪造。

## 读懂证据与检验结果

- 工具返回是带编号的信封：`E3` 是一次读取的观察，`E3.f2` 是其中一条事实。回答和断言只能引用本次运行产生的编号；面板中点击引用可跳转到对应事实，不存在的引用显示为红色未知标记。
- `coverage.complete=false` 或存在 `priority=required` 的 `next` 时，结果只是部分观察；断言写成"完整"会被硬检验拦下。
- 提交回答时运行硬检验 H1–H6（注册于 `verifier` 的 `answer` 套件）：H1 引用必须存在且有对应断言；H2 失败结果只能用于 `scope=unavailable`；H3 部分证据不能支撑完整性断言；H4 断言中的数字必须来自所引证据；H5 证据画布版本与运行冻结版本不一致（致命，直接失败）；H6 调用过工具却没有任何断言。未通过时回答会作为草稿回退修改，面板「复盘」卡片逐项显示每个检查的结果与语义问题；连续未通过受 `MAX_ANSWER_REVISIONS` 限制。
- 前后端共用 `tests/fixtures/agent-contracts/cases.json` 做契约 roundtrip；新增证据种类（artifact/check/approval/validation 为生成链路预留，本期只有 Schema）时必须同时补充正反例。

## 排查 limit_reached

`limit_reached` 表示按预算主动停止，不是故障；错误码在 Run 的 `errorCode` 中：

| errorCode | 含义 | 处理 |
| --- | --- | --- |
| `MODEL_CALL_LIMIT` | 模型调用达到上限 | 拆小问题或提高路由 `maxTokens` 无效，需检查问题复杂度 |
| `TOOL_CALL_LIMIT` | 工具调用（含失败）达到上限 | 缩小选区或简化问题 |
| `FORMAT_REPAIR_EXHAUSTED` | 模型多次未按要求调用控制工具 | 换模型路由或简化问题 |
| `PLAN_REVISIONS_EXHAUSTED` | 重新规划次数用尽 | 查看受阻步骤的 blocker 复盘 |
| `ANSWER_UNVERIFIED` | 回答多次未通过硬检验 | 展开复盘卡片核对 H 检查项 |
| `CONTEXT_LIMIT` | 必需上下文层超出 `contextMaxBytes` | 提高该路由字节数配置或清理会话 |

## 验证

自动测试无需付费供应商。单元测试使用 `httpx.MockTransport`；真实队列测试使用空凭据，验证 Worker 能领取并明确报告未配置凭据，绝不向供应商发送 HTTP。

```sh
npm run verify

docker compose -p aivideo-agent-test -f tests/integration/compose.agent.yaml up -d
AIVERO_DB_URL=postgresql+psycopg://agent_test:agent_test@127.0.0.1:55446/agent_test \
  PYTHONPATH=apps/api .venv/bin/alembic upgrade head
AIVERO_DURABLE_TEST_DB=postgresql+psycopg://agent_test:agent_test@127.0.0.1:55446/agent_test \
AIVERO_DURABLE_TEST_REDIS=redis://127.0.0.1:6386/0 \
AIVERO_REDIS_URL=redis://127.0.0.1:6386/0 \
AIVERO_DURABLE_TEST_BROKER=amqp://agent_test:agent_test@127.0.0.1:5676// \
  .venv/bin/python -m pytest -q tests/integration/test_agent*.py tests/integration/test_canvas_agent*.py tests/integration/test_production_api.py

docker compose -p aivideo-agent-test -f tests/integration/compose.agent.yaml down
```

真实模型验收需单独启用有效路由及凭据：在含图片的画布中询问“画布有哪些图片，第一张的实际尺寸是多少”，展开工具记录核对实际文件尺寸，刷新后确认原 Run 和调用次数保持一致。
