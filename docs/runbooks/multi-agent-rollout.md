# 多 Agent 生产运行与验收

规格：仓库根目录 `TECH_MULTI_AGENT_PRODUCTION.md`。生产入口仍为 FastAPI；`apps/server` 的内存 Host 仅用于开发与离线测试，其生产启动被禁止。

## 实际执行路径

`backend.agent.media` 实现 Host Intent、模板 Planner、严格 DAG 校验、Scheduler 和 Reviewer。对外只有 Host，画布不展示内部模型选择器。图片、四方向、文字视频与参考图关键帧 → 光流视频均经过能力目录；未配置能力明确拒绝，不降级交付静图。

运行、步骤、事件、冻结的能力绑定、审核调用、积分流水和节点租约存入 PostgreSQL。任务受理复用 `services/acceptance.py`，在同一事务中写入预占、占位节点、Job 和 Outbox。Outbox → RabbitMQ → bridge → Celery 复用现有 Agent、图片、视频与导出队列，Redis 使用既有执行设施。Scheduler 修复丢失唤醒；重复投递不会重复提交付费任务。

- DAG 批量报价和积分预占先于任务提交；余额不足整批不启动。每个子任务、重试尝试分别结算，重复或迟到收据只结算一次。
- 跨进程绑定并发容量在数据库事务内检查。四方向独立生成，多帧合成等待所有输入。部分失败保留成功作品，跳过依赖后代；单项重试不重跑成功兄弟。
- 节点租约和 `assetVersion` CAS 保护写回；用户可调整位置，不能替换或删除忙碌节点。前端同步采用三方合并，保留未保存的布局并报告真实冲突。
- 样片确认创建新正片 run，绑定样片来源、种子和新目标节点。图片正片以样片作为编辑输入；文字视频正片为重新生成，界面明确提示结果可能变化。
- 替换请求可显式取消旧 run，并发追加请求可保留旧 run。取消后的输出不写回画布；取消超时释放节点，但在上游结果未确认前保留积分预占。`unknown` 显示待处理，不重新提交。
- 实际文件校验 SHA、尺寸和格式；视频校验时长及真实帧变化。Reviewer 使用实际媒体采样，soft 不通过保留作品和质量提示，hard 不通过停止交付，合规 blocked 隔离资产。
- OIDC、CSRF、工作区成员权限和创建者隔离覆盖 Host API/SSE。事件支持断线回放，状态页恢复确认与进度。

## 配置与启用

1. 对独立环境执行 `alembic upgrade head`（新增 `0013`）。升级前按现有运维流程备份数据库。回退仅适用于旧约束仍兼容的状态，多个用户 Session 或活跃 run 会拒绝不安全回退。
2. 复制 `deploy/host-catalog.example.json`，固定端点、模型、版本、允许尺寸/时长、积分费率和并发。先保持禁用，逐项验证实际 Provider 后启用能力和总开关。Cloud 模型隐式替换、生产 Mock、缺失必需审核路由均拒绝。
3. `.env.production.example` 的 `AIVERO_HOST_CATALOG_FILE` 挂载到 API 和 Agent Worker 的同一个只读路径。API 使用 `AIVERO_PRODUCTION_HOST_CATALOG_FILE`，Worker 同名变量；生产运行环境固定 `production`。默认 `deploy/host-catalog.disabled.json` 不启用 Host。
4. 凭据由 Worker 的既有 credential registry 解析，目录和浏览器不持有密钥。必需审核配置 `reviewer` 的固定 vision route；默认 `reviewMode=off`，不会伪造审核通过。
5. 余额由可信平台账务操作授予。`AIVERO_HOST_CREDIT_GRANT_TOKEN` 为空时禁止授予；启用需 32 字符以上平台密钥及工作区管理员身份。浏览器和普通租户管理请求不能自行充值。
6. 所有服务共享媒体卷；视频合成与质检需要 FFmpeg/ffprobe（现有 Docker 镜像已包含）。本地开发 Mock 须明确使用 development 环境与目录，同时仍使用同一 PG/Celery 架构。

## 接口与排障

主要入口：`POST /api/host/turn`；`GET /api/documents/{id}/host` 与 `/host/events`；`GET /api/host/runs/{id}`、`/ledger`；POST `/confirm`、`/confirm-final`、`/retry`、`/cancel`；GET `/api/host/capabilities`、`/credits`。所有变更沿用会话 CSRF。

模型、端点和输入摘要保留在内部冻结绑定与流水，公开能力仅返回可用性和允许规格。流水可核对 run/step/index/attempt、预占、收费、退款、bindingVersion、providerTaskId 与错误码。不可把 unknown 改为 queued、删除预占或直接补发任务。

`/internal/metrics` 增加 Host 状态、预占数量及年龄、过期节点租约和取消超时指标；`deploy/alerts.yaml` 包含预占积压、租约恢复、结果未知告警。取消后仍预占时先核对上游终态收据，再由正常恢复对账结算。

## 隔离验证

`npm run verify` 包含 TypeScript、构建、eslint、前端测试、创作服务和 Python 检查。`tests/backend/test_host_media_contracts.py` 覆盖生产门禁、严格请求、DAG 与禁用部署示例。

生产集成测试必须显式使用独立的 `AIVERO_DURABLE_TEST_DB`、`AIVERO_DURABLE_TEST_REDIS`，迁移后执行：

```sh
.venv/bin/python -m pytest tests/integration/test_host_media.py tests/integration/test_production_api.py tests/integration/test_agent_api.py tests/integration/test_canvas_agent_runtime.py tests/integration/test_canvas_agent_races.py tests/integration/test_canvas_agent_governance.py -q
```

真实消息投递测试另需独立 `AIVERO_DURABLE_TEST_BROKER`，执行 `tests/integration/test_host_media_delivery.py`。它启动真正的 Celery Worker，经 Outbox/RabbitMQ/bridge 完成四方向生成，验证重复投递只有四个 Job 和一次扣费，使用空凭据与 development Mock。

本轮全仓验证通过（前端 410、创作服务 75、后端 537 项通过）；数据库集成 58 项通过，真实消息交付 1 项通过。

本轮在自建临时 PostgreSQL/Redis/RabbitMQ 中完成数据库集成、真实消息交付、实际多帧 MP4 合成与 `0013` 升降级验证。没有调用付费 Provider，也没有部署或改动现有开发/生产基础设施。真实 Provider 的端点、质量、收费与限额必须在部署目录配置后做上线前烟测；代码测试不等同于这些外部服务的验收。
