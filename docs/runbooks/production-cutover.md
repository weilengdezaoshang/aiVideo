# 持久化后端部署、切换与恢复

这是单机 Compose 部署，不是多节点高可用。正式切换前必须完成
`docs/architecture/durable-execution-status.md` 的剩余验收项；不要直接对现有 data 目录运行导入。

## 配置与启动

使用独立 PostgreSQL、RabbitMQ、Redis 和共享 POSIX 媒体卷。所有 API、Worker 共享同一媒体卷；
只有 API 加载 OIDC 配置，只有生成 Worker 挂载供应商凭据。禁止启动旧 JSON 调度器或 Celery beat。

设置以下部署环境变量，不将值提交到 Git：

- `AIVERO_DB_PASSWORD`、`AIVERO_MQ_PASSWORD`：URI 保留字符需要编码。
- `AIVERO_PUBLIC_ORIGIN`：公网 HTTPS origin，不带尾斜杠。
- `AIVERO_OIDC_ISSUER`、`AIVERO_OIDC_CLIENT_ID`、`AIVERO_OIDC_CLIENT_SECRET`。
- `AIVERO_SESSION_SECRET`：至少 32 字符的独立随机值。
- `AIVERO_ROUTES_FILE`：绝对路径、只读的固定路由配置；Mock 示例见 `deploy/provider-routes.mock.json`。
- `AIVERO_CREDENTIALS_FILE`：绝对路径的版本化凭据映射；Mock 使用空对象。键是 credentialRef，
  值只允许 imageApiKey/videoApiKey。不要删除仍有未结束任务引用的凭据版本。
- `AIVERO_ARTIFACT_ORIGINS`：允许下载的精确 origin，逗号分隔；默认拒绝外部下载。
- 可选 `AIVERO_METRICS_TOKEN`：监控专用随机 Bearer token；未设置则指标接口关闭。

在身份平台登记回调 `<AIVERO_PUBLIC_ORIGIN>/auth/callback`。登录成功不自动取得工作区权限；
由受控运维账号调用 `python -m backend.tools.production_admin grant-member`，提供
`--workspace` UUID、`--issuer`、`--subject`、`--actor`，必要时 `--role admin`。
该操作留数据库审计。API 不提供开发身份绕过。

```sh
docker compose -f compose.execution.yaml -f compose.production.yaml config --quiet
docker compose -f compose.execution.yaml -f compose.production.yaml up --build -d
```

默认各进程 DB pool 为 2、overflow 为 1。按 API 副本、所有 Celery 子进程、调度和投递进程合计
连接上限，另外预留迁移、备份与管理连接。每增加 Worker 并发都必须重新核算。
所有预算可用 `AIVERO_POLICY_` 前缀覆盖；执行期限受理路由使用 imageTimeoutMin/videoTimeoutMin。

## 一次性切换

1. 完成隔离验收；关闭入口新写入，停止旧 API、旧队列及所有可能写旧目录的脚本。
2. 对数据库、完整旧媒体目录和路由配置做同一停写时间点的备份，记录校验摘要。
3. 新数据库运行 `alembic upgrade head`。不要先授予目标工作区成员权限。
4. 导入到新工作区：

```sh
python -m backend.tools.import_legacy --help
python -m backend.tools.import_legacy \
  --legacy-root /absolute/backup/data --media-root /absolute/new/media \
  --workspace '<new-workspace-uuid>' --original-provider-routes /absolute/original-routes.json \
  --actor '<operator>' --quiesced
python -m backend.tools.media_audit --media-root /absolute/new/media
```

导入保留已有 UUID、文档 revision 与可验证生成幂等记录，校验媒体哈希和引用。
已完成输出缺失、源文件改变或跨工作区 ID 冲突均中止导入。
未结束旧任务不能证明未提交时统一 unknown，保留名额；导入本身不发出任何网络请求。
有上游句柄的任务也必须审计确认原路由后再对账。
旧文档创建幂等映射不含原请求参数，当前不迁移它；代理/分镜执行记录等也尚不在该导入器范围内。
启用这些旧功能的部署不得跳过相应迁移验收直接切换。

5. 核对任务/文档/资产数量、媒体审计、前端快照和授权；配置成员后再打开入口。
6. 旧 JSON 只读保留。新系统开始写入后优先前滚。必须回退时先停写、导出新增数据、核对所有
   上游任务与名额，并形成增量迁移方案；禁止用旧快照覆盖新任务。

## 故障处理

- PostgreSQL 不可用：停止新受理/提交，先恢复数据库；不能把未持久化结果作为成功返回。
- RabbitMQ 不可用：Outbox 有界积压。恢复后 Dispatcher 自动续投；超过投递次数的事件为 failed，
  逐条核对后通过死信工具审计恢复。Scheduler 与租约回收不依赖 RabbitMQ 派发自身任务。
- Redis 不可用：暂停新付费提交；API 的新增任务准入仍由 PG 串行控制，SSE 自动周期对账。
- Worker 被杀：等待 Scheduler 回收租约并检查产物收据；不要删除 executionEpoch 或把 submitting 改回 submit。
- unknown：保留上游名额，不要批量重新生成。确认上游已停止后，用
  `production_admin confirm-stopped --job ... --actor ... --evidence ...` 审计释放。
- 有原上游句柄但对账耗尽：确认原 endpoint/模型/凭据版本后，用
  `production_admin resume-reconciliation --job ... --actor ... --evidence ...` 开启一次新的 24 小时审计窗口。
  它只查询原任务，不重新提交，也不重置原执行截止时间。
- 401/403：修复原凭据版本，核对供应商后用 `production_admin unpause-credential --help` 审计恢复。
- 媒体损坏/不可用：先恢复共享卷，再执行 media_audit；下载只取既有产物，禁止重新生成来补文件。
  media_audit 的孤儿项仅供审查，不自动删除，运行任务和未知任务文件受保护。
- FFmpeg 超时：进程组 TERM，10 秒后 KILL；rembg 超时不能强杀线程，实际结束前持续占槽。

## 备份恢复演练

停写后使用 PostgreSQL 原生 `pg_dump --format=custom` 与媒体卷快照做配套备份；
记录备份时间、迁移 revision、路由配置版本、媒体清单。凭据由独立密钥管理备份。
恢复到全新隔离数据库和全新媒体卷，禁止直接覆盖生产实例。
先恢复 PG，再恢复媒体，执行 `media_audit` 和 API 授权/快照验收；所有 unknown 任务先人工核对。
验证完成前不要给恢复环境挂载付费凭据，也不要启动生成 Worker。

## 监控

Prometheus 用专用 Bearer token 抓取 `/internal/metrics`；不要将此 token 发给浏览器。
加载 `deploy/alerts.yaml`，配置真实通知接收方后做告警演练。告警包括 unknown、Outbox 最老年龄、
过期租约和投递失败。指标来自 PG，覆盖跨进程状态；阶段耗时日志包含 jobId、epoch 和 phase。
这里没有宣称已有完整的分布式追踪平台、延迟直方图或告警接收渠道。
