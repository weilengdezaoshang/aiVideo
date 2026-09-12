# 目标架构与当前状态

> 状态:进行中。本文档记录架构升级的当前实际状态与目标差距,随阶段推进更新。
> 基线日期:2026-09-12(以当时工作区为基线,含未提交的 FastAPI 迁移修改)。

## 当前实际架构

```
部署:单进程 FastAPI(fcntl 文件锁强制单实例,apps/api/backend/app.py:107-115)
API:REST + SSE 全部在 create_app() 内注册(app.py);评测路由 /api/evals/v1 独立 router
任务:FIFO 内存队列(deque + asyncio.Task,backend/jobs.py),无持久化租约
持久化:JSON 文件 + 原子写(backend/storage.py、common.py)
  data/documents/{docId}.json   文档(乐观并发 baseRevision,409 冲突)
  data/assets/{assetId}/        素材(original + t256/t1024.webp + meta.json)
  data/images/img_{hex}.{ext}   生成产物 + data/history.json 索引(裁剪上限 500)
  data/jobs.json                任务记录(终态保留 200,跳过带 requestId 的任务)
Provider:mock / comfyui / cloud(百炼),共享 httpx.AsyncClient(app.py:116)
前端:React 19 TSX(workspace/landing/evaluation,esbuild 打包)
      + 原生 Konva UMD 画布(apps/web/canvas,命令式 TS,非 react-konva)
```

## 目标架构(任务书 §二)

```
反向代理 → FastAPI 实例(可多实例)
任务队列 → 独立 Worker(租约/心跳/失联恢复)
API 与 Worker 共享数据库、资产存储与业务代码

后端:Router / Service / Repository 边界 + 统一异常/日志/HTTPX 层
      + PostgreSQL(Workspace/Document/GenerationRequest/Job/Attempt/Asset/AssetReference/Outbox)
      + Redis(缓存、限流、跨实例通知)
      + 统一资产服务(引用管理、GC、宽限期)
前端:React + react-konva + editor-core + 统一 Axios 请求层 + 错误码映射
      + 可靠自动保存、SSE 封装、任务对账
```

## 阶段计划与当前进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 0 基线 | 代码/测试/兼容矩阵/决策记录 | ✅ 完成(本文档 + tdd-progress.md) |
| 1 基础可靠性 | 已知缺陷回归、异常/日志/HTTPX、Axios 层、lint 门禁 | 🚧 进行中 |
| 2 业务边界 | Router/Service/Repository、资产所有权、状态机 | ⬜ 未开始 |
| 3 持久化执行 | PostgreSQL、Outbox、Worker、租约、Redis | ⬜ 未开始 |
| 4 前端收敛 | SSE、自动保存、react-konva | ⬜ 未开始 |
| 5 运行验证 | 权限隔离、多 Worker、故障注入、部署配置 | ⬜ 未开始 |

## 兼容矩阵(阶段 0 盘点)

| 能力 | 现状 | 兼容约束 |
| --- | --- | --- |
| 文档格式 | `{id,name,objects,order,revision,updatedAt[,groups,chat,schemaVersion]}`,≤5000 对象、≤5MB | objects/order 结构与 baseRevision 乐观并发必须保留 |
| 保存幂等 | `Idempotency-Key` 头 → `_idempotency.json`(500 条) | 迁移到 PG 后键需带 workspace 作用域唯一约束 |
| 生成提交 | POST /api/generate 202 + jobId;requestId + requestHash 幂等;队列满 429 | 202/429/409 契约保留 |
| 任务状态 | queued→running→completed/failed;重启后上游任务标 unknown;POST /api/jobs/{id}/reconcile 对账 | 需扩充取消待确认/确认取消;unknown 语义保留 |
| SSE | /api/events:snapshot 首包 + job/image 事件 + overflow + ping | 事件格式与快照对账机制保留 |
| 历史 | /api/history、DELETE /api/images/{id}、PUT star;裁剪上限 500 | 列表契约保留;文件删除必须先查画布引用(见 tdd-progress 第 1 轮) |
| 素材 | /api/assets 上传/列表/元数据/海报;静态挂载 /assets | URL 形态 `/assets/{id}/original.{ext}` 保留 |
| 抠图 | 单推理线程 rembg;operationId 取消;槽位在线程完成后释放 | 语义保留 |
| 评测 | 创建 run 立即 202;ThreadPoolExecutor 后台执行;mock/replay/live 三模式 | run/trial 契约与预算网关保留 |
| 前端草稿 | localStorage `gencanvas.docDraft.{docId}`;GET 成功即清除 | 需改为"保存确认后才清"(已知问题 #2) |
| 部署 | Docker(Node 构建前端,运行期仅 Python);单 worker | 多实例化需引入 PG/Redis 与独立 Worker |

## 已确认的真实缺陷(阶段 0 核实)

1. **历史裁剪误删画布资产**:`History.save` 裁剪(storage.py:256-264)与 `History.remove`
   (storage.py:272-278)删除文件前不检查画布文档引用;画布对象以 `src: /images/img_x.ext`
   直连历史文件(generation-reducer.ts:203-217,assetId 为 null)。→ 阶段 1 第一个 TDD 循环修复。
2. **GET 文档成功无条件清除本机草稿**:canvas/app.ts:303-305。→ 阶段 1/4 修复。
3. **轮询超时被标 failed**:jobs.py:250-252;已获取 externalTaskId 的超时任务失去 unknown
   状态与对账入口。→ 阶段 1/2 修复。
4. **取消 running 任务只停止本地等待**:jobs.py:139-172,仅 ComfyUI 顺带中断上游。→ 阶段 2。
5. **SSE 对账按活跃快照缺席判定任务丢失**:generation-reducer.ts:136-172("任务丢失"错误卡)。
   → 阶段 4。
6. **ruff 6 处预存错误**(evaluation/api.py F841、gateway.py F401/F811 等)导致
   `npm run verify:backend` 失败。→ 阶段 1 修复(需先核实每处)。
7. **tests/backend/test_http.py 真实 TCP 测试无法运行**:子进程缺 PYTHONPATH(迁移回归)。
   → 阶段 1 修复。
8. **两个 flaky 后端测试**(见 tdd-progress.md 基线记录),根因待查。→ 阶段 1 调查。
