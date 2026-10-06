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

1. 执行 `alembic upgrade head`，本功能新增迁移 `0009`。
2. 复制 `deploy/agent-routes.example.json` 到部署配置目录，填入供应商实际可用的模型名、区域/工作区 `baseUrl`、路由标签及 `credentialRef`。地址为 Chat Completions 的基础地址，不含 `/chat/completions`。
3. API 设置 `AIVERO_PRODUCTION_AGENT_ROUTES_FILE`；Compose 使用宿主机 `AIVERO_AGENT_ROUTES_FILE` 映射它。未配置时加载空列表，面板显示暂无可用模型。
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
