# 统一到 8443：移植与退役记录

主工作区为 `aiVideo` / `main`。唯一日常入口是 `https://localhost:8443`，运行 `npm run dev:stack`；生产继续使用现有 Compose、PostgreSQL、RabbitMQ、Redis、Celery。保留 `AgentPanel / AgentRun`、活动流、证据引用和媒体 DAG。

## 模块取舍

| 旧线能力 | 8443 的处理 |
| --- | --- |
| 请求瞬时态协议 | 提取共享 `AgentRequestSchema`，经 `AgentRequest.context` 适配现有 `/api/host/turn`；选区/视口在保存与网络等待前复制，重试复用原请求 |
| 服务端上下文解析 | 在已有文档锁、身份和版本检查内按持久化节点解析；纯文生不带选区参考，显式引用/改图/图片转视频不信任客户端描述和种类；只保留上下文 ID 元数据 |
| 首帧视频适配 | 提取 Cloud `wan2.7-i2v` 序列化与分阶段参考图传递，保留实际 task ID、恢复、取消和产物校验；默认绑定不自动切换到付费模型 |
| 暗色占位图 | 按现有 renderer 层次合入，保留主干连线跟随与分层渲染；视频查看/播放和样片标记继续复用主干实现 |
| 能力目录/绑定、积分、样片正片 | 主干已有实现，复用 `backend.agent.media` 和当前账本；不复制旧 Gateway 或 `creation_*` 账表 |
| 真实文本模型 | 复用服务端 OpenAI 兼容适配器和 credentialRef；已恢复本机工作区对话 API 设置弹窗及保存接口，不启动旧 Node Host |
| Host 持久化、画布结果回填 | 复用主干 AgentSession/AgentRun、Job、Outbox、节点租约和资产 CAS；不启动第二个 Host、不接 `CanvasCreationPanel` |
| 评测、手势等相同改动 | 保留主干现有版本，避免重复移植 |

## 契约与数据库

`context` 是现有 HostTurn 的可选增量，保留原客户端兼容。请求 ID、文档、版本、选择和消息必须与外层一致；来源身份从现有 OIDC/会话获取。上下文不接受客户端身份、权限、历史、recent_ops、旧 canvasContext。附件、整页/截断选区和多页暂不支持，明确拒绝。没有增加新的编辑器 @ 菜单或附件 UI。

不保留旧 `/api/creation/v1/agent/*` 默认入口。旧 Node Host 的上下文历史、self-op 表、限流实现不按表迁移；后续如需扩展，应接主干的运行记录与治理，而不能复活旧会话栈。本次上下文适配不声称实现旧线全部 Node HTTP/存储协议。

仅保留主干 `0013_host_media_dag`。旧 `0013_creation_host_durable`、0014、0015 不导入，因此不产生并列迁移头。未迁移旧 Host 的运行中任务或用户数据；不会在启动时改写旧数据。

## 本地环境与退役

开发模板依旧禁用；启动器生成独立 `data/dev/host-catalog.json`，只允许 development Mock 和本地合成。固定开发工作区首次获得一次 1000 Mock 测试积分，固定幂等键避免重复赠送。生产目录、真实凭据和生产积分均不改动。

每次开发启动构建当前前端并刷新 API/OIDC，避免另一目录或旧构建造成效果不生效。旧线只有在主干验证通过后退出日常启动；保留未提交源码快照以便追溯，不做整仓 merge、不重置原有改动。

## 验收

- `npm run verify`：类型、构建、lint、前端/服务端/后端测试。
- 使用独立的 `aivideo-consolidation-test` PostgreSQL/Redis，执行主干迁移到 0013；`test_host_media.py` 和 `test_production_api.py` 验证实际 Job/资产回填、余额与重放、CSRF、请求上下文和 STALE_VERSION。
- Cloud 首帧协议用 MockTransport 验证真实分阶段请求序列化，不调用真实付费供应商。付费 Provider 的出片、账单与账户限额不在本次验收结论中。


## 2026-10-15 本机验收与归档

- 8443 实际 HTTP + Outbox/Worker：四方向 Mock 样片 `awaiting_user_final` → 正片 `completed`；画布共 8 个资产节点，其中 4 个正片；正片账本 4 条，重复请求复用同一 run，实际 JS 与本地构建 SHA256 一致。
- 证据保存在 `data/dev/consolidation-smoke.json`；可显式运行 `.venv/bin/python scripts/smoke_8443.py`，它只接受本机 55437 数据库和 development Mock 目录，并创建全新的 QA 工作区。
- 独立 PostgreSQL/Redis 的媒体与 HTTP 集成 27 项通过，主干迁移只有 `0013` 一个 head。
- 旧 API 8445、OIDC 8446、Host 8798 已停止。旧工作区在核对源码与盘点快照一致后，用 `git worktree move` 保留到 `~/.codex/backups/aivideo-consolidation-20261015-084435/retired-8445`；原日常目录已移除。忽略的运行文件和全部未提交修改均随目录保留，没有 reset、提交或 force push。
- 这份归档用于追溯与恢复，不是日常启动入口。主工作区的活动流、工具分组、流光和证据跳转测试保持通过。

最终 `npm run verify` 退出码 0：前端 425 项通过，创作服务 75 项通过/1 项环境跳过，Python 554 项通过/2 项跳过。依赖弃用警告 1 条。未调用真实付费供应商。

## 本机对话 API 配置恢复

右上角用户菜单 → 配置 API Key，保存 HTTPS 接口地址、模型、Key、30–60 秒超时。兼容旧 `/api/creation/v1/llm-config` 路径，由 8443 的 Python API 承接，要求 development、已登录的工作区 owner/admin，并沿用 Origin/CSRF 校验。配置按工作区存入共享媒体根下不可通过资产接口访问的 `private-agent-config`，原子写入、文件权限 0600；Key 不回传、不写浏览器存储或运行快照，留空保留原值。

非媒体 Agent 模式的新运行通过 `local-dialogue` 路由读取配置，Worker 仅在 development 解析该凭据引用，保存无需重启。当前默认媒体 Host 使用模板规划，该配置不会自动切换模板规划或更改生图/视频绑定。未移入旧线个人凭据，也未调用真实供应商验证。
