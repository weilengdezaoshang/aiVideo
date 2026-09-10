# AI 视觉创作平台 · 技术实现方案 v0.4

日期：2026-09-09。关联 [PRD v0.4](./PRD-创作平台与画布Agent-v0.4.md)、[五角色评审](./PRD评审提示词-五角色讨论-v0.3.md)、[全栈首轮证据](./reviews/v0.4/02-全栈开发-独立评审.md)。
状态：供评审与按阶段开发的建议蓝图；不表示负责人已批准或生产代码已经实现。实际观察来自当前包含未提交修改的工作区。本轮未安装软件、未调用付费生成；本文的性能、质量和并发阈值均是待验证目标。

## 1. 实施结论与事实边界

建议复用原生 HTML/CSS/JS + Konva 画布，引入 Python FastAPI 作为新领域 API，单机 SQLite 存文档/任务/会话/事件，本地 immutable 文件存资产与蒙版；独立 Python worker 执行任务。先完成持久契约和四方向能力实验，再改入口、四图组、手动蒙版、受限 Agent，最后接视频资产联通。
四图必须是一个父任务和四个固定方向槽；现有单张接口、串行 batch 和四个独立变体不能直接宣称满足。Agent 首例采用“显式单图引用与完整明确意图→真实模型受限计划→服务端校验后自动准备一次蒙版→等待用户→蒙版栏显式抠图→透明 PNG 与落位”。
文生图、文生视频和抠图流程已经验证是用户提供的背景事实；不扩大为四方向、任意供应商、图生视频、重启恢复或 Agent 已通过。最终以 PRD A01–A19 和本方案故障用例验收。后续用户已确认中性灰白、少量蓝色强调、默认浅色且仅编辑器可切深色；这是视觉范围确认，主题 HTML 实现仍仅属于原型，不代表生产落地。最新介绍页的暗色影像展厅是独立章节设计，不跟随编辑器主题。

### 1.1 实际文件复用矩阵

| 文件 / 当前观察 | 复用 | 必须改造 / 不能声称的能力 |
| --- | --- | --- |
| [package.json](../package.json)、[server.ts](../src/server.ts) | Express 静态服务、HTTP 错误结构、测试命令 | 新核心迁到 Python；旧 `/api/generate` 不是组任务协议 |
| [stage.js](../web/canvas/engine/stage.js) | 相机、world/screen 变换、缩放和平移、图层同步 | 模式仲裁、工具栏安全矩形、视口持久化 |
| [object-layer.js](../web/canvas/engine/object-layer.js) | 图像缓存、分级缩略图、选择/拖动、视口裁剪 | 现状主要 image/video/placeholder/error；增加组、画板、文字、八控制点 |
| [commands.js](../web/canvas/state/commands.js)、[doc-store.js](../web/canvas/state/doc-store.js) | 纯命令、逆命令、撤销思想 | 区分本地草稿与已确认命令；服务端 revision、补偿撤销 |
| [document-api.js](../web/canvas/state/document-api.js) | API 封装结构 | `localStorage→最近→新建` 回退须删除，错误不得打开另一画布 |
| [autosave.js](../web/canvas/state/autosave.js) | 防抖与串行请求思想 | 全量覆写改操作提交；旧 ACK 不能把新修改标已保存；beacon 不等于送达 |
| [generate.js](../web/canvas/flows/generate.js)、[app.js](../web/canvas/app.js) | 参数收集、来源引用、局部界面绑定 | `batchCount=1`、四变体循环独立提交、丢参考图时退化文生图均不能沿用到新契约 |
| [generation-events.js](../web/canvas/state/generation-events.js)、[generation-reducer.js](../web/canvas/state/generation-reducer.js) | reducer 和事件先于响应的对齐测试 | 当前只拿 completed 最后一张；active snapshot 缺席判丢失、最近 100 条对账须替换 |
| [job-manager.ts](../src/services/job-manager.ts) | 生命周期、取消入口和测试夹具 | batch `for + await` 串行；重启 queued/running 变 failed；无 documentId、上游请求持久恢复 |
| [provider.ts](../src/services/providers/provider.ts)、[cloud-provider.ts](../src/services/providers/cloud-provider.ts)、[comfyui-provider.ts](../src/services/providers/comfyui-provider.ts) | 请求 fixture、错误转换、工作流知识 | 单张返回；Cloud capacity=2，ComfyUI=1；不能只修改 capacity 就宣称四方向通过；冻结运行时 Provider 配置 |
| [cutout.js](../web/canvas/flows/cutout.js)、[apply-mask.ts](../src/services/cutout/apply-mask.ts) | detect/apply 分离，按钮“抠图”，白保留黑透明 | 提交立即 exit、固定 0.5 进度、失败丢编辑会话；须版本化并任务化 |
| [Python 抠图服务](../services/cutout/server.py) | rembg session、alpha 提取、现有模型配置 | 当前 ThreadingHTTPServer 不是完整 Python 领域后端；模型初始化/推理并发需隔离 |
| [documents.ts](../src/documents.ts)、[assets.ts](../src/assets.ts)、[store.ts](../src/store.ts) | 原始文件、legacy ID、缩略图规格 | JSON 无 revision；资产双存储；旧历史裁剪不能删除新画布仍引用的资产 |
| [tests](../tests)、[README](../README.md) | 本地 fake 上游、像素合成、文档/状态测试 | 测试存在不代表本轮通过；旧“强制 1 张/串行进度”断言需由新合同替换；README 能力要按实测更新 |

## 2. 推荐技术栈、替代方案与工程边界

```text
Browser：原生 JS + Konva，DOM 承载导航/对话/工具栏/textarea/video
   │ /api/v2 REST + 单文档 SSE
Python FastAPI API ── Domain Operations ── SQLite + Event/Outbox
   │                                   └─ 文件资产/缩略图/蒙版
   └─ 独立 Python Worker ── Provider Adapters / 有限 Agent Planner
                         └─ rembg 推理进程 + Pillow 合成
```
FastAPI + Pydantic 用于请求/响应、工具输入和 OpenAPI 契约；HTTP 客户端建议 httpx，SQL 访问可从标准 sqlite3 + repository 开始，迁移规模增长后再用 SQLAlchemy/Alembic。采用锁文件固定验证过的 Python/依赖版本，不在方案阶段猜测某一最新版本兼容全部模型。
FastAPI BackgroundTasks 可执行响应后的小任务，不能直接充当本项目持久队列；官方对重计算也建议考虑独立执行工具。任务恢复、幂等和落位仍由本项目实现。[FastAPI 官方说明](https://fastapi.tiangolo.com/tutorial/background-tasks/)
SQLite WAL 适合当前单机范围：读写可并行、仍只有一个写者；数据库不放网络共享盘。建议 `foreign_keys=ON`、`busy_timeout=5000`、短事务，持久性优先时用 `synchronous=FULL` 并实测。[WAL 官方约束](https://www.sqlite.org/wal.html)
替代取舍：PostgreSQL + Redis/Celery 在多机器 worker、持续写竞争或多用户扩展时再引入；原生 JS 维护成本确实升高时可单独评估 React，不与此次 API/任务迁移绑定。保留 Express + Python 工具服务最便宜，但不满足“Python 承担核心领域”的推荐目标。
服务暴露首版默认本机；API 密钥只在服务端 credential registry，日志/计划/文档只存 credentialRef 和配置版本。能力检查必须区分 configured、reachable、verified；有 Key 不等于鉴权或模型能力已验收。

### 2.1 建议目录（尚未创建）

```text
backend/pyproject.toml             # Python 工程、依赖锁与开发命令
backend/app/main.py                # FastAPI、生命周期、错误响应
backend/app/api/{documents,assets,groups,masks,agent,events}.py
backend/app/contracts/             # Pydantic DTO、工具 JSON Schema
backend/app/domain/{operations,layout,tasks,capabilities}.py
backend/app/repositories/          # SQLite 事务、查询、迁移适配
backend/app/db/migrations/         # 有序迁移 SQL、schema_version
backend/app/workers/{runner,leases,recovery}.py
backend/app/providers/{base,mock,cloud,comfyui}.py
backend/app/tools/{cutout,mask,asset}.py
backend/app/agent/{planner,validator,executor}.py
backend/app/storage/{files,thumbnails,legacy_import}.py
backend/tests/{unit,contract,fault_injection}/
web/intro/{index.html,intro.css,intro.js}  # 产品介绍与效果预览
web/workspace/{index.html,app.js,workspace.css}
web/shared/{theme.css,editor-theme.js}    # 共享视觉 tokens、编辑器偏好与主题作用域
web/canvas/state/{operations,pending-ops,snapshot,events}.js
web/canvas/engine/{groups,text-edit,selection-handles,toolbar-anchor}.js
web/canvas/flows/{generation-group,mask-session,agent-run,video-preview}.js
scripts/{migrate-v2-dry-run,migrate-v2,verify-v2-fixtures}  # 具体扩展名实现时定
```

## 3. 数据定义与最小数据库约束

资产是不可变媒体；画布对象是媒体/文字/画板在某文档的实例；任务是执行意图；结果组是四个创意槽；派生图插入显示序列但不扩充四槽。会话、消息、ToolCall 保存引用和来源，不能把来源仅编码在文件名或坐标。
下列是核心 DDL 蓝图，可作为迁移起点；上线前需在所锁 SQLite 版本执行验证。UUID 形状、URL、JSON 内联合类型与跨字段语义由 Pydantic/domain 补充校验。SQLite 的外键需要每个连接显式启用。[官方外键说明](https://www.sqlite.org/foreignkeys.html)

```sql
PRAGMA foreign_keys=ON;
CREATE TABLE documents (
 id TEXT PRIMARY KEY, name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
 schema_version INTEGER NOT NULL DEFAULT 2, revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
 viewport_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE operations (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, document_id TEXT REFERENCES documents(id),
 idempotency_key TEXT NOT NULL, body_hash TEXT NOT NULL, kind TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('accepted','applied','failed','uncertain')),
 request_json TEXT NOT NULL, result_json TEXT, base_revision INTEGER, ack_revision INTEGER,
 created_at TEXT NOT NULL, UNIQUE(scope,idempotency_key)
);
CREATE TABLE assets (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('image','video','mask')),
 mime TEXT NOT NULL, content_hash TEXT NOT NULL, storage_key TEXT NOT NULL UNIQUE,
 byte_size INTEGER NOT NULL CHECK(byte_size>0), width INTEGER CHECK(width>0), height INTEGER CHECK(height>0),
 duration_ms INTEGER CHECK(duration_ms>=0), source_asset_id TEXT REFERENCES assets(id),
 metadata_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL CHECK(status IN ('ready','missing','quarantined')),
 created_at TEXT NOT NULL
);
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id),
 kind TEXT NOT NULL CHECK(kind IN ('group_image','mask_detect','cutout_apply','llm_plan','video_generate')),
 status TEXT NOT NULL CHECK(status IN ('accepted','preparing','queued','dispatching','running',
 'unknown','reconciling','completed','partial','failed','cancelling','cancelled','needs_user')),
 stage TEXT NOT NULL, progress REAL CHECK(progress IS NULL OR progress BETWEEN 0 AND 1),
 input_json TEXT NOT NULL, provider_snapshot_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT, lease_until TEXT, cancel_requested_at TEXT, error_json TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX tasks_dispatch ON tasks(status,created_at);
CREATE INDEX tasks_document ON tasks(document_id,created_at);
CREATE TABLE result_groups (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id), prompt TEXT NOT NULL,
 directions_json TEXT NOT NULL, status TEXT NOT NULL, retry_of_group_id TEXT REFERENCES result_groups(id),
 layout_json TEXT NOT NULL, layout_revision INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
 UNIQUE(id,document_id)
);
CREATE TABLE generation_slots (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES result_groups(id),
 direction_index INTEGER NOT NULL CHECK(direction_index BETWEEN 0 AND 3),
 title TEXT NOT NULL, prompt TEXT NOT NULL, constraints_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','running','ready','failed','cancelled','unknown')),
 accepted_asset_id TEXT REFERENCES assets(id), UNIQUE(group_id,direction_index),
 CHECK(status!='ready' OR accepted_asset_id IS NOT NULL)
);
CREATE TABLE generation_attempts (
 id TEXT PRIMARY KEY, slot_id TEXT NOT NULL REFERENCES generation_slots(id), attempt_no INTEGER NOT NULL CHECK(attempt_no>=1),
 provider_key TEXT NOT NULL, external_request_id TEXT, dispatch_state TEXT NOT NULL,
 request_hash TEXT NOT NULL, output_asset_id TEXT REFERENCES assets(id), error_json TEXT,
 dispatch_at TEXT, accepted_at TEXT, finished_at TEXT, UNIQUE(slot_id,attempt_no)
);
CREATE TABLE objects (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 kind TEXT NOT NULL CHECK(kind IN ('image','video','frame','text','placeholder','error')),
 asset_id TEXT REFERENCES assets(id), source_object_id TEXT REFERENCES objects(id), group_id TEXT,
 x REAL NOT NULL, y REAL NOT NULL, width REAL NOT NULL CHECK(width>0), height REAL NOT NULL CHECK(height>0),
 z_index INTEGER NOT NULL, data_json TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0,
 created_by_operation_id TEXT NOT NULL REFERENCES operations(id), deleted_at TEXT,
 UNIQUE(id,document_id), FOREIGN KEY(group_id,document_id) REFERENCES result_groups(id,document_id),
 CHECK(kind NOT IN ('image','video') OR asset_id IS NOT NULL)
);
CREATE TABLE group_display_items (
 group_id TEXT NOT NULL REFERENCES result_groups(id), object_id TEXT NOT NULL UNIQUE REFERENCES objects(id),
 sort_key INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('creative','derived')),
 source_slot_id TEXT REFERENCES generation_slots(id), PRIMARY KEY(group_id,object_id), UNIQUE(group_id,sort_key)
);
CREATE TABLE placements (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), output_index INTEGER NOT NULL CHECK(output_index>=0),
 document_id TEXT NOT NULL REFERENCES documents(id), asset_id TEXT REFERENCES assets(id), object_id TEXT REFERENCES objects(id),
 source_object_id TEXT REFERENCES objects(id), status TEXT NOT NULL CHECK(status IN ('pending','applied','suppressed','needs_placement','reverted')),
 applied_operation_id TEXT REFERENCES operations(id), reverted_by_operation_id TEXT REFERENCES operations(id),
 UNIQUE(task_id,output_index), CHECK(status!='applied' OR object_id IS NOT NULL)
);
CREATE TABLE mask_sessions (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id), source_object_id TEXT NOT NULL REFERENCES objects(id),
 source_asset_id TEXT NOT NULL REFERENCES assets(id), status TEXT NOT NULL,
 latest_revision INTEGER NOT NULL DEFAULT 0, submitted_revision INTEGER, active_task_id TEXT REFERENCES tasks(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE mask_versions (
 session_id TEXT NOT NULL REFERENCES mask_sessions(id), revision INTEGER NOT NULL CHECK(revision>=1),
 asset_id TEXT NOT NULL REFERENCES assets(id), content_hash TEXT NOT NULL, width INTEGER NOT NULL CHECK(width>0),
 height INTEGER NOT NULL CHECK(height>0), created_at TEXT NOT NULL, PRIMARY KEY(session_id,revision)
);
CREATE TABLE conversations (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL UNIQUE REFERENCES documents(id)
);
CREATE TABLE messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), role TEXT NOT NULL,
 content_json TEXT NOT NULL, references_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
 CHECK(role IN ('user','assistant','tool','system_status'))
);
CREATE TABLE agent_runs (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id), conversation_id TEXT NOT NULL REFERENCES conversations(id),
 source_message_id TEXT NOT NULL UNIQUE REFERENCES messages(id), status TEXT NOT NULL,
 plan_json TEXT, limits_json TEXT NOT NULL, reference_snapshot_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX one_active_agent_per_doc ON agent_runs(document_id)
 WHERE status IN ('planning','plan_ready','preparing_mask','awaiting_user','applying');
CREATE TABLE tool_calls (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(id), step_index INTEGER NOT NULL,
 tool_name TEXT NOT NULL, input_json TEXT NOT NULL, output_json TEXT, status TEXT NOT NULL,
 operation_id TEXT UNIQUE REFERENCES operations(id), error_json TEXT, UNIQUE(run_id,step_index)
);
CREATE TABLE tool_attempts (
 id TEXT PRIMARY KEY, tool_call_id TEXT NOT NULL REFERENCES tool_calls(id), attempt_no INTEGER NOT NULL CHECK(attempt_no>=1),
 operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id), task_id TEXT UNIQUE REFERENCES tasks(id),
 status TEXT NOT NULL, error_json TEXT, created_at TEXT NOT NULL, UNIQUE(tool_call_id,attempt_no)
);
CREATE TABLE continuations (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(id), session_id TEXT NOT NULL REFERENCES mask_sessions(id),
 token_hash TEXT NOT NULL UNIQUE, action TEXT NOT NULL CHECK(action='apply_cutout'),
 state TEXT NOT NULL CHECK(state IN ('active','consumed','revoked')), consumed_by_operation_id TEXT UNIQUE REFERENCES operations(id),
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE event_outbox (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 document_id TEXT REFERENCES documents(id), entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
 entity_version INTEGER NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(entity_type,entity_id,entity_version,event_type)
);
```
补充必须在 domain transaction 验证的约束：组提交恰好四个 index 0..3；对象/group/session/run 同属一个 document；sourceObject 对应提交时 sourceAsset；mask 类型与源尺寸一致；已终态 Slot 不被旧 attempt 覆盖；placement 不从 reverted/suppressed 自动回 applied。几何值必须有限、尺寸在能力上限内，不能将 NaN/Infinity 写入；每种状态字段在合同中使用封闭枚举。
不是每个状态都可互转；状态转移在 domain 服务按预期 version 更新，`UPDATE ... WHERE version=? AND status IN (...)` 受影响行为 0 时按冲突处理。外部 ID 不能只做全局 unique：某批量 API 四槽可能共用一个 batchId，需按 provider + batchId + slotIndex 解析。
资产引用保护还需 `asset_references(owner_type,owner_id,asset_id)` 或等价可重建索引，覆盖对象、任务输入/输出、消息引用、蒙版版本。清理只回收确认无引用且过宽限期的文件；不能复用旧历史条数裁剪来删除它们。

### 3.1 前端快照 JSON 示例（示例数据）

```json
{
  "document": {"id":"doc-a","revision":18,"name":"素材练习","schemaVersion":2},
  "objects": [{"id":"obj-1","kind":"image","assetId":"asset-1","groupId":"group-1","x":0,"y":0,"width":512,"height":512}],
  "groups": [{"id":"group-1","creativeSlots":["slot-0","slot-1","slot-2","slot-3"],"displayOrder":["obj-1","cutout-1","obj-2","obj-3","obj-4"]}],
  "conversationId":"conv-a","activeTaskIds":["task-1"],"openMaskSessionIds":["mask-a"],"eventCursor":241
}
```
真实 ID 为 UUID；示例短 ID 只为说明。Snapshot 只带元数据和 asset URL，不内嵌媒体字节、完整撤销栈、密钥或模型推理内部文本。消息可分页，但引用未完成任务与暂停 session 的摘要必须随首次恢复可用。

## 4. API、页面操作和事件合同

所有以下接口以 `/api/v2` 为前缀，属于建议新增；现有 `/api` 不能冒称实现。统一错误 `{error:{code,message,retryable,recoveryAction,operationId?,taskId?}}`，区分确定失败与提交结果不明。

| 页面操作 / PRD | API | 事务结果 / 前端状态 / 事件 |
| --- | --- | --- |
| 切画布 Tab / A01 | `GET /documents?cursor=&limit=30` | 列表/空/错误，零创建；summary 带封面和更新时间 |
| 新建 / A01 | `POST /documents` + Idempotency-Key | 201 返回文档与 operation；超时 uncertain，查原 key |
| 打开/刷新 / A02 | `GET /documents/{id}/snapshot` | doc + revision + cursor；404 不存在，503 读取失败 |
| 改名称/移动/文字/画板 / A02 | `POST /documents/{id}/operations` | baseRevision、commandId；200 ACK / 409 冲突；`document.changed` |
| 上传 / A14 | `POST /assets` multipart + key | 解码通过才 201；image PNG/JPEG/WebP，建议 40MiB；取消不加对象 |
| 资产选择 / A14 | `GET /assets?kind=&cursor=` | 返回 ready 资产；导入用 `add_asset_object` 文档命令 |
| 能力发现 / A16 | `GET /capabilities` | 配置/连通/已验状态、限制、不能使用原因；无隐式生成测试 |
| 生图 / A04–A06 | `POST /generation-groups` | docId、prompt、size、operationId；202 一 task/group/四槽 |
| 重生一组 / A06 | `POST /generation-groups` + retryOfGroupId | 新 key、新组，复用固定方向快照，旧组完整保留 |
| 查任务/不确定 / A12 | `GET /tasks/{id}`、`GET /operations/by-key/{key}?scope=` | 查询无外部创建副作用；回原资源，不创建 replacement |
| 取消 / A12 | `POST /tasks/{id}/cancel` + key | 202 cancelling 或确定 cancelled；并列返回 upstreamStopConfirmed |
| 识别蒙版 / A07 | `POST /mask-sessions` + key | sourceObjectId/sourceAssetId；202 识别 task，结果 mask 不是透明图 |
| 存蒙版 / A08 | `PUT /mask-sessions/{id}/versions/{revision}` | PNG + hash + priorRevision；相同版本不同 bytes 409 |
| 显式抠图 / A07 | `POST /mask-sessions/{id}/apply` + key | 指定已存 maskRevision；202；Agent 路径还需 continuation |
| 查看蒙版草稿 / A08 | `GET /mask-sessions/{id}` | 最新/已提交版本、状态、活动 taskId；不重识别 |
| 发 Agent 消息 / A10–A11 | `POST /conversations/{id}/messages` + key | message 持久引用后排 planner；返回 runId；合法完整单图计划自动创建唯一识别 task |
| 识别确定失败后重试 / A11 | `POST /agent-runs/{id}/retry-mask` + key | 用户明确动作；计划/引用复验，新识别 attempt；unknown 不开放此接口 |
| 暂停恢复 / A11 | `GET /agent-runs/{id}` | 计划、ToolCall、待用户步骤；查询不重启 planner/tool |
| 检查主体选区 / A11 | `GET /agent-runs/{id}/continuation` | 仅当前客户端取得等待步骤凭据；过期需显式 POST `/continuations/renew`，不重跑识别 |
| 手动放置保留结果 | `POST /documents/{id}/operations` | `place_retained_output`，复用 asset，独立幂等 placement 操作 |
| 视频预览 / A15 | `GET /assets/{id}`、`GET /assets/{id}/content` | poster/duration/source + 媒体 Range；原生 video 控件 |
| 下载 / A14 | `GET /assets/{id}/download` | 原文件 Content-Type/Disposition；开始下载不等于已保存磁盘 |
| 事件恢复 / A12 | `GET /documents/{id}/events?after=<cursor>` | SSE id、event、data；过期水位发 reset_required，客户端重取 snapshot |

生成提交建议 body：`{operationId,documentId,prompt,size:{width,height},placement:{anchor,groupGap:24},retryOfGroupId?}`。方向由服务端规划器产生；`retryOfGroupId` 时加载原冻结方向，不重复规划来改变失败恢复语义。用户改需求用全新创作请求。
需要靠引用创建任务时，如原图刚上传尚未保存对象，前端先等 `add_asset_object` ACK 再发送有执行意图的消息；不能把未存在的 objectId 给 worker。单纯生成锚点只提供坐标，不需要先保存一个客户端占位。

## 5. 幂等、修订与任务落位

### 5.1 创建意图与 body hash

前端在网络请求前生成 operation UUID、持久本地 pending 记录，所有同一次重试复用同 key。按钮禁用减少误操作；真正去重在服务端，不依赖前端 loading。相同 scope/key 的 body hash 不同返回 `409 IDEMPOTENCY_CONFLICT`。
规范化 body：先按 schema 校验/填默认值，规范名称的 trim；键按固定顺序序列化，对数组保持顺序；hash 包含操作类型、API contract version、documentId 和实质输入，不包含时间戳、traceId、baseRevision 等运输重试字段。原始 body 与 canonical form 都有版本说明，避免部署后默认值改变导致同 key 被误判。
`baseRevision` 作为并发前提单独核验：第一次尚未执行且发生 revision 冲突，保留 operationId 和意图 hash，允许用户/前端重取基线后以相同意图重新提交；一旦 applied，则先返回既有 ACK，不再检查当前 revision。body 的实质命令改变须新 key。
`POST /documents` 在事务中创建 document+operation+conversation，commit 后才回复。响应丢失可查 key；查无记录但原请求仍可能在途时，复用同 key 重发也只能创建一份。操作记录与创建资源同生命周期保存，不能几分钟 TTL 到期就重新允许执行。

### 5.2 前端 revision 与后台完成共存

前端状态分 `serverSnapshot + pendingCommands + transientUI`，最终画面为按序重放 pending；transient 包含选择、加载动画、画笔预览，不直接污染服务端文档。推荐 1 秒防抖、笔画结束存草稿，具体目标经性能试验确认。
服务端 `BEGIN IMMEDIATE`→核对 document revision→应用命令→revision+1→写 operation ACK 与 event_outbox→COMMIT。SQLite 事务保证的是该连接中的本地修改原子性；上游 HTTP 不能放在事务中。[SQLite 事务文档](https://www.sqlite.org/lang_transaction.html)
409 时保留本地草稿，拉新 snapshot；拖动 delta 可针对未删除对象重放，文字/名称等覆盖型编辑先按对象 revision 检测，冲突明确展示原/本地内容，不静默覆盖。连续移动/缩放只在前端合并，ACK 顺序不能靠响应到达顺序猜测。
“已保存”条件是最新本地 revision 对应命令全部 ACK。旧快照保存成功但存在新 pending 时保持“保存中”；后台新结果也在服务器 revision 中。beforeunload 只作尽力 flush，可靠返回按钮要等待 flush Promise/ACK；失败留页面或明确放弃。

### 5.3 创建四图任务与落位原子性

第一笔事务建立 Operation、父 Task、ResultGroup、四 Slot、四稳定 placeholder object、用户 Message 和回复引用；worker 只领取已 commit 的 task。占位使用预生成 ID，事件先于 HTTP 响应时也可按 operationId 对齐。此时方向字段可为显式未规划状态；planner 完成后同事务填齐四方向并冻结，planning 未结束前不得派发图像请求。
worker 保存产物时先写临时文件→解码、尺寸/MIME/hash 校验→原子 rename→事务写 Asset/Slot。发生崩溃的孤儿文件可回收，已持久 Asset 不需重复下载/生成；数据库不得引用未完成 rename 的文件。
四槽确定终结后，短事务统一发布组状态、替换 placeholder、计算布局、更新 document revision、写 Message 引用、Placement 和 event。task 完成且 placement 未完成要显示两种状态；数据库重试只能重落位，不重调用模型。
`UNIQUE(task_id,output_index)` 防止重复落位；`placements.status` 阻止完成事件复活撤销对象。输出已生成但源图删除时为 needs_placement，Asset 保留；用户显式“加入画布”创建新的放置 operation，不改写原已撤回记录。

## 6. Atomic outbox、SSE 游标与恢复

本方案 event_outbox 同时是已提交领域事件日志；API/worker 只在业务事务内插入，SSE 从表按 sequence 读取，不靠内存 EventEmitter 作为唯一来源。首版无需消息中间件；以后增加消息分发才另加 delivery cursor，不要把某客户端收到当作全局已消费。
快照过程：在同一个只读事务视图内读取 document、对象、组、依赖任务、会话摘要和 MAX(sequence)，一起返回 `eventCursor`。随后订阅 after=cursor；期间发生的更新都在日志中，可以补发，不存在“先取快照再注册回调”漏事件窗口。
消息格式如下；MDN 定义了 `id/event/data`、自动重连和重试间隔，但不提供本项目数据库持久或业务去重保证。[MDN SSE 说明](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)

```text
id: 242
event: group.updated
data: {"documentId":"doc-a","entityId":"group-1","entityVersion":4,"documentRevision":19,"operationId":"op-a","status":"completed"}

```
SSE 连接按当前 documentId 过滤，心跳注释建议 15 秒；同一个页面只开一个文档流。首次重建 EventSource 用 query after；原生重连可读 Last-Event-ID，服务端验证它属于有效水位。反向代理关闭流缓冲，设置 no-cache，不为每次进度新建连接。
客户端记最后已应用 sequence，并按 entityVersion 忽略旧事件；通知包含足够的小型 patch 或要求重取 entity 的 revision。重复/乱序通知不得直接 append 图片对象。完整实体因离线跨版本时优先快照恢复。
建议事件保留 30 天或可配置空间上限，水位过期返回 reset_required；Task/Slot/Placement、文档和关键工具记录不能随事件短保留期丢失。活动任务列表仅用于徽标，不用于判断完成/丢失；恢复查询文档依赖的具体 taskId，避免最近 100 条截断问题。
服务全部离线只承诺当前内存/IndexedDB 已缓存内容，原文件未缓存则显示不可读；前端不在此阶段增加全量 Service Worker 离线产品。草稿写失败要明确提示，不能假称刷新可恢复。

## 7. 四方向能力、整组调度与部分失败

### 7.1 能力合同与阻断门槛

```json
{"providerId":"configured-profile","revision":3,"textToImage":true,
 "creativeGroup":{"count":4,"distinctPromptPerSlot":true,
 "mode":"parallel_requests","maxConcurrentRequests":4,"verified":false,
 "progressSource":"none","supportsQuery":false,"supportsIdempotency":false,
 "supportsCancel":false,"evidenceRecordId":null}}
```
合法 mode 为 native_multi_prompt_batch、parallel_requests、unsupported；配置值和真实证据分开。当前 Cloud capacity=2、ComfyUI=1 且接口单输出，全部视为新四方向能力未验；Mock 可验证流程但不能被选为真实发布验收。旧生成实验入口可保留明确标识，新入口禁止悄悄用它降级。
“原生 n=4”只有在确实接受四个不同方向的条件或可证明生成方向表达时才合格；一个 prompt 加四 seed 不自动合格。Python 并发 HTTP 也只能证明同时派发，不能证明第三方 GPU 同时运算。
建议验收定义：同一个调度窗口不等待任何结果就发出四方向请求，或一个可验证的四方向原生 batch；保留 dispatch/accept 时间和上游 ID。这是本轮对“同时生成”的工程验收解释，不是对既有约束的静默放宽。供应商内部推理是否重叠仍待验证；若需证明这一层，必须做有可观测 worker 的 Provider 实验，不从 HTTP 推断。

### 7.2 方向规划与执行

planner 输出 `{commonConstraints, directions:[{index,title,variationAxis,prompt}]}`，必须正好四项且 index 0..3；固定主体、用途、用户明确限制，变化构图/光线/场景等允许轴。冻结规划 JSON 后才派发图像请求；失败则整组 planning_failed，无图像副作用。
首版不用用户预审四份复杂表单，但可展开方向说明；重新生成同需求的一组复用固定方向快照，新 seed 可不同并记录。编辑需求则新规划。质量需要人工看实际结果，不能仅验证 JSON 不重复就宣称四方向合格。
调度先检查 Provider profile 最大能力；不足 4 且无 native batch 的任务应 capability_unavailable，不能永远排队。并行 mode 领取整个组的 4 个容量许可；不足时整个组等待，不能先跑两张再等两张。单 worker 首版可用内存加持久租约，重启按未知请求保守占用对账。
有多个调度器时，Provider lease 的加权预留要在短事务内核对 `sum(active weight)+requested <= capacity`，一次申请 4 或一次全拒绝；不顺次 acquire 四个 semaphore 造成半组占有。配额、速率限制和供应商并发是不同限制，需分别处理。
并发协程独立捕获可恢复错误，完成一个槽不停止其他槽；`gather(return_exceptions=True)` 或 TaskGroup 子任务边界捕获可用。TaskGroup 未处理异常会取消其余任务，不适合直接用于“尽量保留 1–3 张结果”的语义。[Python 官方协程文档](https://docs.python.org/3/library/asyncio-task.html)
首版不按单图结果推百分比；真实采样 step 可保存诊断，但组 UI 只显示准备、等待、执行、不定加载。所有槽有确定终态后统一发布四图或 partial；实际收到的文件可以先落盘抗崩溃，尚在执行的组不逐张揭示 1/4 进度。

### 7.3 部分失败与重生

终态归并：4 ready→completed；1–3 ready 且其余确定 failed/cancelled→partial；0 ready 且全部确定终结→failed/cancelled；存在任何 unknown→结果待确认，不编造缺席槽失败原因。父 task execution status 与 placement status 独立。
首版 partial 只提供查看失败、使用已得素材、显式“重新生成一组 4 张”；新 task/group/key，retryOfGroupId 关联旧组，旧成功不复制/覆盖。UI 不称免费重试，也不暗示仅补缺席方向。
补失败槽延后；虽然 DDL 提前保留 attempt 表用于请求追踪，修复还需 attempt 接受版本、迟到结果隔离、单槽配额、合并终态与收费说明，不能只加一个按钮。当前每槽默认一个 attempt，不做自动质量重抽。

## 8. 外部请求不确定、取消与 exactly-once 边界

任务本地幂等 ≠ 第三方 exactly-once。provider 接收请求与本地写 externalRequestId 不共享事务：若接收后响应丢失或 worker 崩溃，上游可能已收费但本地不知道。自动 retry 会产生第二次真实调用。
派发前保存 dispatch intent/request hash；收到上游 ID 立即持久化；可透传幂等 key 的 Provider 使用相同 attempt key。HTTP 客户端对创建请求默认不开通用自动重试；可重试 GET/下载需明确不发起新生成。
重启处理：未曾进入 dispatching 的 queued 可重新领取；已 dispatching/running 先变 reconciling；有 query ID 查原任务，无 query 能力则 unknown/needs_user。只允许恢复确定性的本地文件校验/数据库落位，不借恢复重新调 LLM/生成模型。
超时分三类：服务端确认未接受→可安全重提同意图；明确生成失败→用户新尝试；接受结果未知→只查询/等待人工处置。不能按 elapsed 时间自行把 unknown 变 failed。真正要另起新创作需明确是新请求，不承诺避免旧请求费用。
取消先记 cancelRequestedAt；queued 且未派发可以确定 cancelled；已派发调用 Provider 能力，只有明确回应才显示停止已确认。不支持取消时停止后续步骤和自动落位关注，已生成资产保留；取消既不意味着退款，也不删除成功输出。
ComfyUI 全局 interrupt 等行为必须单独验证能否影响其他任务；多用户或共享 GPU 时不能假装它是精确 task cancel。切换 Provider 配置只影响新任务，运行任务持久 provider revision/模型参数并继续向原请求查询。

## 9. 蒙版坐标、版本、alpha 与显式提交

### 9.1 模式状态与输入

手动图下“抠图”只创建 MaskSession 并识别；Agent 对单图完整明确意图，经真实 LLM 计划与服务端校验后自动走同一个识别服务，至多一次自动识别。`detecting→editing→saving_revision→applying→completed/apply_failed`，识别失败独立 detect_failed；蒙版和透明图是不同 Asset.kind。
原图解码时一次性处理 EXIF orientation，记录归一化像素宽高；蒙版必须对应同一 sourceAsset 内容 hash、方向、宽高，禁止自动拉伸错误尺寸蒙版。普通图片允许 source alpha，透明输出仍保留它。
前端 pointer 经过 `screenToWorld→对象逆变换→原图像素坐标`；不支持旋转/翻转的首版把这些变换禁用。对当前等比图：`px=(worldX-objectX)/objectW*sourceW`，Y 同理；笔刷大小明确按世界/屏幕定义并映射，不能混用。
画笔/橡皮对原图尺度 mask 操作；快速移动使用相邻采样连线插值，pointer capture 保持笔画。显示层半透明高亮，“高亮区域将保留”；UI 棋盘格只表示透明，不能烘进导出文件。源图背景仍可见。
蒙版灰度 white=keep/black=remove；局部重绘 white=repaint 是另一种 semantic enum，接口/工具不能共用未标语义的 `maskImage`。初版辅助选择未验则不开放。

### 9.2 草稿、显式 apply 与像素算法

每个完整 stroke 进入蒙版 undo 栈，并写 IndexedDB 草稿；周期性 raster checkpoint + stroke 增量避免每次 pointermove 保存整幅 RGBA。跨刷新至少恢复最近已持久化栅格版本；不承诺还原全部本地 undo 历史。
服务端 MaskVersion 为 immutable PNG，hash 标识提交内容。PUT 相同 revision/hash 可重复成功，不同 hash 409；新 revision 必须基于 latest。mask 版本上传失败允许继续本地编辑，但 apply 不可被误认为已提交。
点击蒙版栏“抠图”冻结 revision，先等该版本保存 ACK，随后原子消费 continuation（若 Agent）并创建 apply Task；前端锁定本次编辑和重复提交，按钮“生成中…”，原图/蒙版叠层保留。聊天、退出、笔画、恢复不能触发该端点。
像素合成建议 `Aout=round(Asource*M/255)`；保留源 RGB，不把原透明像素恢复不透明。灰度 M 先按明确色彩规则转换，不能误把 RGB 的 red 通道当任何蒙版；尺寸、通道、最大像素数通过验证后在 Pillow 输出 PNG。
该 apply 是确定性合成，不需要再调用生成大模型。进程崩溃可以按同 task、相同输入 hash 继续确定性处理；成功输出以唯一 task/output 关联，不新增重复透明资产记录。合成质量仍需软边/发丝/半透明物体样本验收。
失败保留源 Asset、MaskVersion、error 和 taskId；“返回蒙版”只回 editing。用户未修改又重新点按钮属于明确的新 apply attempt，仍使用新的操作 key；网络未知则查旧 task，不能直接解锁新 apply。

### 9.3 退出、源图移动/删除与撤销

退出默认先保存草稿并返回普通选择，保存成功轻提示“草稿已保留”，不每次增加确认。只有保存失败才显示继续编辑/重试保存/明确放弃草稿；成功保存才称已保留。识别中退出可请求取消或停止关注；晚到 mask 只存 session，不自动重新打开编辑器或生成透明图。
源对象移动不改变 mask；完成落位读取源对象当前几何。源 asset 被替换则旧 session 不再直接 apply，提示重选/重新识别；仅对象缩放保留原 asset 时可继续，像素蒙版仍按原分辨率。
源对象删除采用 tombstone，任务输入 asset 不删。输出存资产并标 placement=needs_placement，消息“结果已生成，尚未加入画布”；用户手动加入后才可认为放置步骤完成。完成时若源对象已改用另一 asset 也采用此分支；禁止复活源图或把结果落到最近选中图片旁。
撤销结果插入写新的 compensation operation：移除该结果对象和本次布局影响，保留资产/任务，并记录 placement=reverted。若用户撤销了 pending 组占位，设置 suppression，迟到产物入库但不复活组；取消上游为独立尽力动作。
补偿布局依据当前顺序移除目标并重新排，而非覆盖一份旧全组 snapshot；避免撤销抠图结果时把后续用户缩放/移动也倒回。必要时 object revision 冲突暂停补偿并提示，不伪造成功。

## 10. 组布局、工具栏、文字与画板

固定 creativeSlots 0..3；displayOrder = 源图→较早派生→新派生→下一原方向，gap=24 世界单位；新结果追加在该源图派生链末尾。派生角色带 sourceSlotId，组完成仍显示“4/4 + N 个抠图结果”。重生是新组，不能把第五张变成新方向。
受控横排按显示对象真实宽度累加 x，纵向顶对齐；抠图源宽高与源图显示尺寸一致。组标题/边框可拖整组，单图仅选择/等比缩放；单图变化重算后续布局，首版不自由拖散或拆组。
源图保留位置；新派生在源图右侧派生链末尾插入，后续组员顺延。其他组/独立对象不自动移动；跨组碰撞给出定位/整理提示。重放布局带 layoutRevision，多个结果近同时到达由服务器事务串行排列，派生排序键用 apply task 持久创建时间及 ID 作为并列次序，不能用客户端收到事件时间。若较早任务晚返回，按既定顺序插回链内并顺延。更新 display_items 的排序在同一事务中重建该组序列，避免逐行改 sort_key 命中即时 UNIQUE 冲突。
画布新派生沿用源图显示尺寸，扩展组容器而不压缩原四候选矩形；对话四图预览只读 creativeSlots，抠图另建单图结果卡。不能将 CSS `repeat(4,1fr)` 改为 `repeat(5,1fr)` 后挤小所有图片冒充组内顺延。
单图选框贴边，四角圆点与边中胶囊、左上图片标签；控制点维持屏幕点击尺寸，所有点等比缩放，结束后写 width/height 并归一化 scale；不修改 asset 像素。组标题点击与图片/边框命中区明确分离。
DOM 工具栏按世界包围盒投影到屏幕，优先图下居中；左右 clamp 到排除顶栏、四工具、对话、输入框的安全矩形（输入框位于对话栏底部；对话收起或窄窗折叠时回落画布底部居中，2026-09-10 修订）。下方不足先轻移视口，仍不足停靠画布安全区底部并显示对象名，文字/点击区不随画布缩小。
相机变化/ResizeObserver/选择变化在同一 rAF 批次更新；对象完全离屏隐藏跟随栏并保留定位入口。普通栏与蒙版栏互斥，点击栏不清选择；任务成功若用户在编辑其他对象则不夺焦点，仅通知可定位结果。蒙版栏右侧“抠图”按钮固定，左侧工具区横向滚动，主按钮不得卷出可视区。
文字用 Konva.Text 呈现，双击时 DOM textarea 同位置编辑；支持 composition/中文输入，Ctrl/Cmd+Enter 完成、Esc 恢复本次内容、空文本离开移除空对象。默认系统字体 32px，多行黑字，样式保存为纯数据。[Konva 官方编辑示例](https://konvajs.org/docs/sandbox/Editable_Text.html)
画板默认 1024×1024，在视口中心创建可命名空间框；不裁剪、不自动收养相交对象、不承诺合成导出。画布、文字、蒙版三套 undo/快捷键作用域，IME 期间不触发全局发送/删除；视频控件内部拖动不冒泡为画布拖动。

### 10.1 已确认的主题边界与原型实现

四页面职责保持清楚：介绍页说明产品与展示效果；工作台组织模块、最近文档与画布入口；我的画布负责创建/打开文档；编辑器负责生成、素材编辑和对话。工作台和列表始终浅色，仅编辑器可以切换深色。介绍页为贯穿首屏与后续章节的暗色影像展厅，章节外观不受编辑器偏好控制。主题不是新的业务模式，不改变四图、蒙版、Agent 或保存 API。

共享 CSS 自定义属性采用语义命名，禁止外层产品壳、iframe 和工具栏分别硬编码不同主题值。当前原型由 `docs/reviews/v0.5/visual-src/theme.css` 统一提供，构建时注入外层与 srcdoc；生产方案在 `web/shared/theme.css` 复用同一语义。

| Token | 默认浅色 | 编辑器深色 |
| --- | --- | --- |
| `--studio-bg` | `#F5F5F7` | `#191A1E` |
| `--studio-panel` | `#FFFFFF` | `#25262C` |
| `--studio-ink` | `#202124` | `#F0F1F4` |
| `--studio-accent` | `#356DF3` | `#356DF3` |
| `--studio-muted` | `#60646C` | `#B2B6C0` |
| `--studio-line` | `#DEDFE4` | `#41444F` |

实现合同：

1. 编辑器偏好值仅允许 `light` / `dark`，无值或非法值回退 `light`。单独使用 localStorage key `aivideo-editor-theme-v1`；不写入 CanvasDoc、draft、pending operations 或任务快照，不增加 document revision。
2. 有效主题按 `currentPage === editor ? editorPreference : light` 计算。离开编辑器恢复产品浅色 tokens，不覆盖偏好；介绍页局部暗色影像展厅由 `landing.css` 自身控制，不是编辑器深色模式。重开编辑器复用已有偏好。存储拒绝时维持内存中的本次选择，不声称已经持久化。
3. 切换只修改根 DOM 的 `data-editor-theme` / `color-scheme` 及按钮可访问状态，CSS tokens 负责更新外观；不能重建 iframe、替换编辑器根节点、调用全量 `render()`、重新加载文档或执行状态初始化。焦点环/选框等呈现可更新，源资产像素、文字对象的持久样式和导出内容不能因主题改写。
4. 自包含原型使用 `{type:'product-theme', theme:'light'|'dark'}` 从外层同步 iframe。iframe 校验 `event.source === window.parent`、消息类型和枚举值；外层校验返回消息来自自己的 frame。由于 srcdoc/file 原型使用 postMessage，当前连接方式不是生产架构要求；生产同源部署应收紧到明确 origin，或使用共享前端状态而不保留 iframe。
5. 主题不触发 `generate` / `apply` / `respond`、不重新打开蒙版会话、不重连 SSE，不改变 `selected`、`reference`、输入草稿、mask strokes、任务 ID、组 slot、对象位置或视口。四候选与派生布局仍按原来的固定尺寸与 24 世界单位间距执行。

原型实现与生产范围分开记录：共享 theme.css、`product-theme` 消息及独立偏好键用于当前独立 HTML 的体验同步；`web/shared/` 的生产落地与真实 Konva 画布主题适配属于后续实施。若 Konva 的辅助节点不能从 CSS 自动更新，只对非作品 UI 的颜色属性执行最小更新与 `batchDraw()`，不得重建 Layer/Stage 或清空编辑状态。

主题生产验收计划（本机原型已执行的抽样验证见 [主题同步检查](./reviews/v0.5/主题同步检查.md)，不代替全量验收）：A17 检查默认浅色、只编辑器深色、工作台/列表浅色、介绍页章节外观不联动、偏好重开恢复与存储拒绝；A18 在未发送文本/引用、单图选择、蒙版已有笔画、生成中和 unknown 状态切换，比较切换前后状态值与任务提交次数。补查双主题下选框、蒙版控制、失败/未知提示、透明棋盘、键盘焦点与正文对比；截图应标明页面和主题，旧浅色视觉结论不得自动沿用为深色通过。

## 11. 真实 LLM 受限计划与可恢复 Agent

### 11.1 引用快照与 planner 输出

用户消息持久化 `{documentId,objectId,assetId,assetHash,displayName,thumbnail}`；选择 B 不改已发送 A；planner 只拿允许引用句柄 `ref_1` 与元数据，不直接枚举全库。首版一个引用；无引用/多引用应澄清，未接视觉模型不能说看到了服装、身份或图像细节。
一次消息仅一次模型规划，保存模型/提示词版本、结构结果、延迟和 provider requestId；不保存或展示模型隐藏思维。模板只可作加载/错误文案，不可替代真实 LLM plan 验收。格式无效明确失败，首版不自动多次纠正/重试。

```json
{
 "type":"object","additionalProperties":false,
 "required":["decision","reference","steps","question"],
 "properties":{
  "decision":{"enum":["plan","clarify","unsupported"]},
  "reference":{"enum":["ref_1",null]},
  "question":{"type":["string","null"],"maxLength":300},
  "steps":{"type":"array","maxItems":3,"items":{"type":"object","additionalProperties":false,
   "required":["tool","reference"],"properties":{
    "tool":{"enum":["prepare_subject_mask","await_mask_submission","place_cutout_result"]},
    "reference":{"const":"ref_1"}
   }}}
 }
}
```
domain validator 再要求 decision=plan 时 steps **必须依次且恰好**为上述三项；clarify/unsupported 时 steps 为空。示例 schema 面向单引用；实际服务端根据已发送引用构造句柄枚举，引用数不是 1 时禁止 decision=plan。JSON Schema 形状通过不代表安全计划；校验 document、引用有效、能力、run 状态、调用次数及来源句柄映射，不相信模型自填 ID。
计划卡显示“只处理此图、准备蒙版、由你确认、新增一张透明素材到右侧、原图保留”。仅一个有效显式引用且用户完整意图都在首例范围时，经服务端校验自动创建最多一次识别；不增加额外“准备蒙版”点击。混合超范围需求不能删去不支持部分后擅自执行，必须先澄清。
合法 plan、run/ToolCall 状态与唯一识别 Task 在同一事务落盘，避免重复计划事件/页面重连再次识别。模型/计划刷新不重跑；若准备完成时用户已切换到文字或另一蒙版模式，只显示“检查主体选区”入口，不抢焦点、当前选择或编辑模式。

### 11.2 工具白名单、暂停与 continuation

模型可提议的三个工具不是三个任意 API 权限：executor 固定顺序，只能对 run 的 ref_1；`await_mask_submission` 为持久挂起，`place_cutout_result` 只能消费前一步已生成输出。没有 shell、文件路径、任意 URL、删除资产、跨文档操作或背景生图工具。
识别完成事务写 MaskSession + MaskVersion + ToolCall completed + run awaiting_user + continuation。凭据由随机 continuation ID、过期时间和服务端 HMAC 签名构成，token hash 存库；服务可按持久记录重建同一凭据供当前画布客户端读取，避免仅存 hash 后无法恢复明文。绑定 run/session/action/source hash，建议有效 24 小时；签名密钥仅在服务端，token 不给 LLM、不进模型上下文或 SSE 广播。
过期 token 不重跑识别/模型；用户恢复同一等待状态，经引用复验、显式 renew POST 重新签发并撤销旧 token，普通 GET 不修改状态。apply 接收 `{maskRevision,operationId,continuationToken}`，一个事务检查 token active、session/source、run version，再 consume token 与创建 task。重复同 operation 返回原 task，不因 token 已 consumed 报错。
蒙版编辑可增加 revision，token 绑定 session/source 而不预先锁最初 revision；点击时服务端验证提交 revision 已存在且为此次当前确认版本。聊天“继续”只能返回定位蒙版的 UI action，不能调用 apply 或消费 token。
挂起不占 worker；刷新 GET run 返回原计划/步骤/待确认位置，无新模型调用。来源失效时 needs_user，不替换引用；另发新需求先结束未执行步骤形成新 run，不暗改已执行计划。
首例限制建议：同文档至多一个非终态 run；一次模型规划、最多一次自动识别尝试、一个用户确认后 apply、零文生图/视频调用、零自动质量重试。确定识别失败可用户显式重试并在 tool_attempts 记新 attempt/operation/task，原 ToolCall 保持同一步而不抹去失败历史；unknown 不重试；额度记录不与实际计费金额混淆。

### 11.3 错误样例与 Agent 验收

| 输入/异常 | 合法行为 | 禁止结果 |
| --- | --- | --- |
| “抠这张图”，无引用 | clarify，请添加一张图片 | 猜最近选中/最新上传并执行 |
| 引用两图，“抠出来” | 请指定一张，保留可见引用 | 批量处理或静默选第一张 |
| “抠图后再生成十个背景” | clarify/unsupported，说明当前范围，等用户明确新的完整意图 | 多轮生图扩大调用或擅自只执行其中抠图 |
| “抠图并生成背景”混合需求 | clarify，解释可支持范围，等用户明确意图 | 静默删掉背景步骤后自动识别 |
| 模型输出未知 tool / 非法 reference | PLAN_INVALID，零工具副作用，原消息保留 | 直接调用函数或拼 URL |
| 等待时用户说“继续” | 定位蒙版栏，提醒点击“抠图” | 自动透明合成 |
| 等待时刷新 | 恢复同 run/session/mask | 重调 LLM/识别或自动 apply |
| 资产已生成但源对象删除 | 结果保留、needs_placement，可显式加入 | 宣告整个任务成功或复活源对象 |
| 模型超时且请求不明 | 保留 run 状态和原引用，查询支持范围 | 自动再次付费请求生成同计划 |

验收用真实模型的受限计划、测试 fake planner 的错误边界、故障注入的暂停恢复三类证据；模型返回合理话术不能替代操作成功。run completed 至少要求合法 OutputRef、Asset ready、placement applied、对话可定位同一对象；其他情况用 partial/needs_user。

## 12. 视频资产与原文件下载

首版保留已验证文生视频入口；结果统一登记 task/prompt/asset，然后从资产库导入指定画布。无输入图时来源为文字描述，不能用当前选中图伪造 i2v；现有 Cloud `generateVideo` 明确未接入，具体 Provider 逐项显示能力。
video Asset 增加容器/MIME、时长、像素尺寸、posterAssetId；已有文件先测媒体元数据提取，必要时后续引入 ffprobe/ffmpeg 并锁版本，本轮不安装。poster 失败可显示可访问媒体图标和说明，不能把视频当一张成功图片。
画布默认只画海报，不多路自动播放；播放键/双击打开原生 `<video controls>` 弹层，单击选择、拖边框移动；关闭暂停和释放资源，焦点回原对象，播放器快捷键不传给画布。
媒体 content 支持 Range、正确 MIME/长度；download 提供清理后的文件名和 attachment。PNG 下载原 alpha，棋盘格仅 UI；视频原文件不转码，普通图片保留原尺寸/格式。不存在/不可读 Asset 返回明确错误，不启动假下载。
文字、画板、整张无限画布、批量 ZIP 不进入首版导出接口；不把截图或浏览器下载触发当作文件已保存到磁盘。

## 13. 单写者迁移、数据校验与回滚

这里“单写者”指每个领域只有一个持久化所有者，不表示只允许一个进程访问 SQLite。API 和 worker 经同一 domain/repository 事务规则写；旧 Express 不能继续覆盖已迁移文档 JSON。

| 阶段 | 流量与所有权 | 迁移操作 | 回退边界 |
| --- | --- | --- | --- |
| M0 契约冻结 | 现有 Node 继续运行；Python 只测 fixture | 记录旧 API 样本、文件 hash、ID/来源映射，创建 schemaVersion=2 | 无数据写切换，可直接停止 Python 实验 |
| M1 Python 独立底座 | `/api/v2` 仅临时数据与测试文档 | Mock/本地 fake HTTP、迁移 dry-run；不把生产生成双跑用于对照 | 删除测试数据不影响原目录 |
| M2 短暂停写 | 禁新建/文档变更/新任务，排空旧执行器 | 备份 JSON、图片、资产、jobs 与 config 引用；校验备份可读 | 未切换前仍可恢复 Node 原目录 |
| M3 导入新库 | Python 写新目录，旧目录只读 | 同 legacy ID 导入 doc/object/asset；生成谱系 task 映射；缺失文件入 missing，不丢对象 | count/hash/ref 校验失败不切流量 |
| M4 新画布切换 | 新编辑器仅 `/api/v2`；Express 暂作静态/同源代理 | 禁止 v2 文档经旧 `/api/documents/:id` 写回；记录 owner/version | 新写入产生后不能简单覆旧备份；停写后导出增量或保持 v2 只读 |
| M5 Provider 迁移 | 新 Task 归 Python；旧运行任务不跨执行器搬迁 | 逐个迁移 Mock、一个真实图像 Provider、视频适配；旧实验结果只幂等导入资产 | 配置切换只影响新 task，旧 task 向原 Provider 查询 |
| M6 去除兼容层 | Python 拥有所有新核心领域 | 全部验收通过后归档旧 API，保留只读映射/迁移记录 | 版本回滚必须支持新 schema 或先做反向转换验证 |

dry-run 输出文档数、对象数、资产数、媒体 hash、缺失/重复/不可解码清单、legacy imageId→assetId、jobId→taskId；源文件不覆盖，不修改密钥。相同输入重复迁移不新增记录，migration_batchId + legacy_key 唯一。
真正迁移时 worker 排空/未知任务隔离；不能重新提交旧 running job 来让新系统“恢复”。旧 JSON 截止时刻与库导入水位一起保存。服务重启验证同 ID 同顺序/名称/引用/媒体可读，视觉抽检代表性文档。
新库数据库备份用受支持的 backup/checkpoint 流程，不能运行中只拷 main.db 忽略 WAL；媒体 immutable，按 manifest 校验。回滚演练在副本做，确认能查看切换后的新产物再决定入口回退。

## 14. 分阶段文件任务与验收

| 阶段 | 具体文件工作包（建议） | 前置 | 完成证据 |
| --- | --- | --- | --- |
| P0 能力/契约 spike | `backend/tests/contract/`、`docs/evidence/capabilities/`；四方向 planner schema、fake Provider | 负责人确认范围 | 单父四槽、并发时间线、官方限制与真实小样计划；未验 Provider 有阻断 |
| P1 Python 持久底座 | `backend/app/db/`、`repositories/`、`domain/operations.py`、`api/documents.py`、`api/events.py` | 实体/幂等约定 | 新建丢响应、重复 key、hash 冲突、revision/事务/outbox 测试，A01/A02/A12/A13 |
| P2 资产/迁移预演 | `storage/`、`api/assets.py`、迁移脚本；保持 `src/store.ts` 旧流量隔离 | P1 | hash/count/ref dry-run；引用资产不随历史裁剪；A14 |
| P3 介绍页、入口和画布基本操作 | `web/intro/`、`web/workspace/`、`web/shared/{theme.css,editor-theme.js}`、`document-api.js` 替换、`state/operations.js`、text/frame/toolbar 模块 | P1/P2 | 介绍页场景/大图/章节导航到工作台、稳定地址、保存/返回、左栏四项、文字/画板、等比控制点；默认浅色、仅编辑器深色与偏好；A01–A03/A13/A17–A19 |
| P4 四方向任务 | `providers/base.py`、`workers/runner.py`、`domain/tasks.py`、groups API、前端 group flow/reducer | P0 实际 capability、P1 | 一次提交四槽、容量整体预留、partial/new group、无假进度、闭页落位；A04–A06/A12/A16 |
| P5 手动蒙版 | `tools/mask.py`、`tools/cutout.py`、masks API、`flows/mask-session.js`、layout 服务 | P2/P3/P4 组布局 | 按钮之前零 apply，蒙版版本/alpha/失败恢复/源图移删/撤销；A07–A09 |
| P6 受限 Agent | `agent/planner.py`、validator/executor、tool_calls/continuations、对话 UI | P5 完整工具 | 真实 LLM plan、澄清/错误、合规意图只自动准备一次/显式抠图、暂停刷新零重跑；A10/A11/A12 |
| P7 视频/下载与收尾 | 视频元数据/海报、video-preview、download、legacy 入口指向、README | P2/P3 + 已验证视频 Provider | 文生视频来源、导入/播放/重开/下载；A14/A15；迁移回滚演练 |

每阶段先最小 vertical slice 再扩到全部状态；不先搭通用插件市场、无限工具注册或多人协作。图像四方向实测失败时其发布门槛保持未满足，可以继续验证导航/蒙版，不用 Mock 成功充数。
Node 仍维护期间执行仓库 `npm run verify`；Python 建立独立 lint/type/pytest 门禁后纳入总 verify。视觉 DOM/键盘走查、真实模型质量与单元测试分开报告，避免测试总数替代产品验收。

## 15. 重点难点、实验与目标指标

以下只是建议起始目标；需记录硬件、模型/版本、图片尺寸、样本集、网络和测量方法，当前没有实测结果。指标未过可以调整范围或优化，不能改验收文案伪称通过。

| 实验 | 设计与失败注入 | 建议目标 / 发布条件 |
| --- | --- | --- |
| S1 四方向并发 | fake Provider 四调用 barrier；真实 Provider 请求时间线；容量=2/4；一调用 429 | fake 四调用在任一完成前均开始；真实 dispatch 窗口建议 ≤1秒且无等待前图完成；不足能力明确阻断 |
| S2 方向质量 | 10 个需求×4 图，主体/用途/风格硬约束、不同构图/光线；人工逐项标注 | 必须每组四条可解释方向；建议起始 ≥8/10 组满足硬约束与明显差异，再按失败原因评审；这是目标非事实 |
| S3 外部未知窗口 | 上游接受后断响应、worker kill；有/无 requestId/query/idempotency 三种 Provider | 无未经用户明确新意图的再次创建调用；unknown 保留；真实是否可查记录证据，不承诺普遍 exactly-once |
| S4 本地事务/事件 | commit 前/后断开、SSE 丢包/重复 10 次/乱序、水位过期、最近列表截断 | 同 task/output 至多一个有效 placement；snapshot/重开最终同 revision；旧事件不复活撤销对象 |
| S5 保存竞争 | 拖动保存中后台插图、双标签 rename 冲突、磁盘满/IndexedDB 拒绝 | 没有假“已保存”；有新 pending 则旧 ACK 不清 dirty；资产和本地草稿保留且冲突可继续 |
| S6 蒙版像素 | 黑/白/灰 mask、透明源、EXIF 旋转、尺寸不匹配、4K、快速拖笔 | 白/黑/50% alpha 符合定义；提交前 0 输出；失败回编 hash 一致；不自动拉伸坏 mask |
| S7 蒙版交互性能 | 指定设备 4K 源图，画笔轨迹与 UI 帧记录，10 分钟编辑 | 交互笔刷响应 P95 <50ms 作为目标；pointermove 不整图网络上传；无无限增长栈 |
| S8 画布性能 | 100/500 混合对象，缩放/平移/组插图；统一设备/分辨率 | 100 对象平移目标 ≥50 FPS；500 对象 P95 交互 <100ms；未达优化缓存/裁剪，不夸大规模 |
| S9 Agent 计划 | 20 条真实模型意图含正常改写/无引用/多引用/越界；fake malformed tool | 禁止调用场景零副作用；有效计划目标 ≥90%；所有 awaiting_user 刷新不重跑；真实费用另外记录 |
| S10 迁移/回滚 | 混合 legacy 文档、缺失媒体、重复运行迁移、切换后新建数据 | 相同 ID/hash/顺序/来源可对账；损坏可见；不丢切换后数据；无 JSON/SQLite 双写 |
| S11 视频/下载 | 文生视频无源图、Range seek、坏媒体、透明 PNG 下载 | 来源准确、播放操作不拖动对象、关闭暂停；下载字节 hash 与 Asset 一致 |

诊断最少字段：traceId、operationId、documentId、taskId、groupId、slotIndex、attemptId、providerRevision、externalRequestId、stage、elapsed、errorCode、resultAssetId、documentRevision；默认脱敏 prompt/用户文件名，费用只记录真实账单或明确标注估算。
错误分类：validation、capability_unavailable、provider_auth、rate_limited、provider_failed、submission_unknown、asset_invalid、mask_mismatch、document_conflict、placement_pending、storage_failed、plan_invalid、reference_stale。恢复动作由错误类型给出，不统一展示“一键重试”。
难点优先级：外部调用未知窗口与幂等边界 > 后台落位/前端 revision 竞争 > 四方向真实能力与质量 > 蒙版版本/坐标/透明边缘 > Agent 人工暂停/恢复 > 画布大对象性能；每项均需可复现样例而非“技术上可做”。

## 16. 方案收敛与未决边界

遵循主持第二轮最终收敛：文字默认 32px，派生按创建顺序追加源图派生链、间距 24，工具栏下方不足采用视口调整/安全区停靠；合规单图完整意图经真实计划校验后自动准备一次蒙版，默认保存草稿退出，最终仅蒙版栏显式抠图生成结果。首版不补失败方向，刷新不重跑已规划/已提交步骤。与首轮或中间草稿不同的默认值已以此统一。
仍需真实验证的是 Provider/model 能力、四方向质量、模型计划可靠性、媒体兼容/性能和迁移恢复；这些不是新增产品决策请求。短用例、视频范围与迁移顺序由 PRD 的三个负责人问题收敛。
所有核心代码路径与协议 fixture 要有对照证据；评审 HTML 的模拟成功、角色共识、官方框架文档都不能替代本应用真实完成条件。本文给出实现蓝图，不改变生产功能状态。
文档静态校验：已将 DDL 在内存 SQLite 执行、解析三个 JSON 示例并检查本地链接；这只验证草案语法/引用，不是后端实现、约束完整性或业务验收通过。

### 页面补充：介绍页与独立产品原型

[当前独立 HTML v0.5](./产品效果-创作平台-v0.5.html) 呈现介绍页→工作台概览→画布列表→编辑器。介绍页为静态展示与预览切换，不调用模型。生产落地由 `web/intro/` 承载 `/`，工作台概览承载 `/workspace`；画布列表继续使用 `?tab=canvas`，创建文档仍由既定幂等 API 负责。默认概览替代此前默认列表的推荐，入口变化不修改文档、任务、Agent 或蒙版接口。HTML 内 iframe、postMessage 与本机主题偏好仅用于自包含原型；其中已同步的视觉与主题行为不等于生产前端已经落地。后续主题测试结果单独记录，不改写旧历史评审结论。

#### 暗色影像展厅实现补充

用户拒绝上一版介绍页主题并指出底部白色操作区突兀后，本轮实现暗色影像展厅作为可评审方向；异步主题偏好尚未回复，不视为用户已确认。范围仅介绍页，工作台、列表与编辑器不随本轮扩大修改。

用户要求参考已观察到的 [即梦官方介绍页](https://jimeng.jianying.com/) 首屏作品构图，后续功能区围绕本项目组织。实现保留 aiVideo 品牌及已有 Unsplash 展示素材；不嵌入即梦媒体，不移植其视频、社区或其他能力宣称。工作台、列表、编辑器和既有业务状态机保持原有行为。

| 原型源文件 | 职责 |
| --- | --- |
| `docs/reviews/v0.5/visual-src/landing.html` | 三层满屏作品首屏、左侧两行标题、左下横排缩略图与说明、首屏仅顶部创作入口，无白操作卡或伪输入框；四张素材卡片、实际原型编辑器截图、静态 Agent 步骤、收尾和 native dialog |
| `docs/reviews/v0.5/visual-src/landing.css` | 作品遮罩和文字可读性、全页深炭灰与暖白按钮、响应式标题与网格、大图布局、减少动态效果分支 |
| `docs/reviews/v0.5/visual-src/landing.js` | 三场景解码与防乱序切换、一次入场及可见性生命周期、选中态和说明同步、作品大图打开/关闭与焦点返回 |

1. **场景与素材**：场景键只覆盖 `ocean` / `architecture` / `flower`。三张图片预置各自 `src`，点击先 `decode()`，检查请求序号及页面可见性后更新 `.is-scene-active`、`alt`、`aria-hidden`、说明与 `aria-pressed`；四张素材画廊另含 `tea`。切换只改变介绍页 DOM，不写画布/任务状态、不创建文档、不调用模型。截图 `EDITOR_PREVIEW` 来自本项目实际原型，作为可点击静态预览；`example` 入口继续由原有本机示例流程处理。
2. **大图与键盘**：使用 `<dialog>` 的 `showModal()` 打开，保留原生焦点约束与 Esc 关闭；另提供关闭按钮。打开前记录触发卡片，`close` 后且当前仍为介绍页时恢复焦点。进入工作台或示例前关闭大图，导航后不把焦点拉回已隐藏介绍页。标题、替代文本和展示素材说明与当前卡片同步。
3. **章节与产品路由**：`#create-images`、`#create-canvas`、`#create-assistant` 是介绍章节，`#intro` / `#workspace` / `#canvas` / `#editor` / `#example` 是原型产品路由。壳层区分两类 hash；介绍页内章节定位不创建文档。从编辑器跳到介绍章节或其他产品页时，仍发 `product-navigate` 并经 iframe 内 `go()` 的离开保护，收到 `product-route` 后再显示页面与定位章节；不能直接隐藏 iframe 绕过未保存/蒙版草稿处理。生产地址继续按第 3 节契约实现，原型 hash 不是新的后端路由要求。
4. **构建与资源**：既有 `build.py` 将 landing 三文件注入产品壳，并把 `VISUAL_ASSETS`、本地字体、SVG 图标与编辑器截图内嵌到独立 HTML。运行不依赖外链图片或字体；素材来源链接仅供主动查看，不下载或依赖即梦资产。场景无自动轮播，页面无自动播放媒体或生成调用。
5. **动态与窄屏**：默认内容静态可见。首屏仅首次进入以 WAAPI 展示图片 1000ms 缩放与标题两行 620ms 错开入场；图库及实际界面图通过 IntersectionObserver 首次进入视口时各播放一次。三层场景图片先 decode，再以请求序号防止快速点击乱序，切换 active class 触发 650ms opacity 过渡，同时同步 aria-hidden、说明和选中态。后台、离开介绍页或 pagehide 时取消动画、断开观察并作废待完成切换；减少动态偏好即时取消动画和过渡，重新允许动态不重播首屏。悬停放大仅在 hover:hover 且 pointer:fine 时启用，无自动轮播。手机首屏保持“让灵感 / 自由成形。”两行，核对目标视口不溢出、入口不遮挡与大图可关闭。

A19 检查首屏顶部单入口、无白操作卡及伪输入框、静态默认、一次入场及后台/离开/reduce 即时取消、精细指针悬停边界、三场景防乱序和选中态、四卡片大图与 Esc/焦点返回、实际截图进入示例、章节 hash 与离开保护、窄屏和减少动态效果。实现与实际浏览器检查的证据单独记录于 [即梦参考介绍页检查](./reviews/v0.5/即梦参考介绍页检查.md)；此次文档同步不宣称这些项目已经全部通过，不改写旧评审结论。上述仍是 HTML 原型实现，生产前端未在本轮落地。

本轮浏览器证据与角色复核见 [暗色展厅检查](./reviews/v0.5/暗色展厅检查.md)、[主题与动效复核](./reviews/v0.5/主题与动效复核.md)、[暗色展厅视觉评审](./reviews/v0.5/暗色展厅视觉评审.md)；此前即梦参考检查为上一版记录。

#### 字体与过渡同步（独立原型）

介绍页标题独立使用 `GalleryDisplay`：官方 Noto Sans SC 实例化为真正 500 字重的 `display-medium.ttf`；编辑器沿用原 `display.ttf`。首屏 h1 字距为 `-.015em`；章节 h2 字重 500、字距 0，字号按 `landing.css` 响应式规则缩放（章节 `clamp(32px,3.4vw,48px)`，桌面常见约 40–48px；收尾 `clamp(32px,4vw,52px)`）。桌面章节正文 16px / 1.8，手机首屏副标题 14px、章节正文 14px / 1.85；素材说明、截图注释与页尾备注统一 12px。 构建分别嵌入标题 Medium 子集与编辑器原字体，来源和授权记录在 `visual-src/assets/sources.json`。

大图预览新增 200ms 进入、150ms 退出，以 opacity 与 scale(.98) 配合遮罩同节奏变化；Esc 与关闭按钮共用退出流程，完成后恢复触发卡片焦点。反复打开时取消旧动画，并用序号防止旧关闭回调关闭新预览；减少动态偏好下直接关闭。章节采用 CSS 原生 `scroll-behavior:smooth`，减少动态时为 `auto`；跨页返回介绍章节平滑定位，返回介绍首屏即时定位。跨页仍经过既有离开保护。本轮不增加工作台淡入。 `dialog` 保留原生焦点约束，原生 cancel 事件接入统一关闭流程；序号失效后旧动画完成回调不得更改当前 dialog 状态。

范围仅独立 HTML 原型，不改变生产功能状态；检查见 [字体与过渡同步检查](./reviews/v0.5/字体与过渡同步检查.md)。

### 介绍页导航与跨页过渡实现

导航采用 fixed 和 84px/72px 对应 scroll-margin-top。被动 scroll 监听通过 requestAnimationFrame 合并，只读取三个章节位置并更新 is-scrolled、aria-current；离开页面取消待执行帧。标题/助手入场复用一次性 IntersectionObserver 与已有 easeOut。产品壳 reveal 先切换可见页面，再在从 intro 进入产品时执行 200ms opacity WAAPI；后续切页取消旧动画，减少动态偏好变化时取消产品动画。该实现仅属于独立 HTML 原型。验证见 [章节过渡检查](./reviews/v0.5/章节过渡检查.md)。
