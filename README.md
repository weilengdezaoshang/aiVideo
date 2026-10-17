<div align="center">

<a href="https://weilengdezaoshang.github.io/aiVideo/">
  <img src="docs/designs/github-pages-2026-09-22/jimeng-v2/readme-cover.png" width="1200" alt="FRAYUNE 帧屿集：让灵感，成为下一帧。月光下的发光蝠鲼与蓝色海洋，原创 AI 幻想视觉。" />
</a>

# 帧屿集 FRAYUNE

让灵感，成为下一帧。

连接外部云模型的开源 AI 视觉创作平台。统一组织图像生成、画布编辑与视频创作，让灵感成为作品。

[浏览项目主页](https://weilengdezaoshang.github.io/aiVideo/) · [快速开始](#快速开始) · [功能概览](#功能概览) · [开发文档](AGENTS.md)

[![CI](https://github.com/weilengdezaoshang/aiVideo/actions/workflows/ci.yml/badge.svg)](https://github.com/weilengdezaoshang/aiVideo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Node](https://img.shields.io/badge/node-%E2%89%A520-green.svg)](https://nodejs.org/)
[![TypeScript](https://img.shields.io/badge/TypeScript-strict-3178C6.svg)](https://www.typescriptlang.org/)

云模型 API &nbsp; / &nbsp; 统一创作工作台 &nbsp; / &nbsp; React + TypeScript · Python + FastAPI

</div>

---

## 从一个想法，到一段作品

| 01 / 图像 | 02 / 画布 | 03 / 视频 |
| --- | --- | --- |
| **让文字，有了画面。** | **给灵感，展开的空间。** | **让下一帧，动起来。** |
| 文生图、参考图生图与局部重绘，逐步调整理想画面。 | 编排素材、检查抠图蒙版、保存创作文档。 | 参考图生成视频，单轨时间线整理片段，导出 MP4。 |

> 帧屿集通过外部云模型 API 提供生成能力，无需在本地部署图像或视频生成模型。平台负责工作台、任务与素材管理，支持自行部署。具体能力取决于接入的云模型；Mock 仅用于开发演示。项目主页和封面为展示内容，不是在线生成服务。

<details>
<summary>阅读导航 · 配置、接口与工程说明</summary>

- [快速开始](#快速开始) · [抠图与导出](#抠图与导出) · [源码开发](#源码开发)
- [云模型接入](docs/百炼生图接入.md) · [模型配置](#模型配置)
- [API 一览](#api-一览) · [项目结构](#项目结构) · [测试与校验](#测试与校验)
- [运行架构与兼容范围](#运行架构与兼容范围) · [时间线与 MP4 导出](#时间线与-mp4-导出)
- [品牌资源](docs/brand/frayune/README.md) · [项目主页维护](website/README.md)

</details>

## 代码与部署状态

新版平台源码已同步到本仓库：`apps/api` 为 Python/FastAPI 服务，`apps/web` 为 React/TypeScript 工作台与画布。云模型通过服务端 Provider 接入，API Key 不写入官网或浏览器静态资源。

[GitHub Pages 官网](https://weilengdezaoshang.github.io/aiVideo/) 仅提供产品介绍。运行创作平台请按下方步骤部署应用并配置云模型；发布源码不代表云 API 服务已经在线部署。

### 创作入口

所有启动方式共用 PostgreSQL / RabbitMQ / Redis / Celery 后端。百炼模型通过固定路由与 Worker 凭据接入，详见[配置步骤](docs/百炼生图接入.md)。后续多项目共用 Key 的规划：[统一模型网关方案](docs/统一模型网关方案.md)。

当前创作入口为 `/workspace` 与 `/canvas`，旧版独立生成面板已下线。

工作台 `/workspace` 与画布 `/canvas` 使用 React + TypeScript，画布渲染基于 Konva，公共组件位于 `apps/web/ui`。`npm run dev` 自动构建并监听前端；生产构建运行 `npm run build:web`。组件拆分、素材与验证说明见 [React 创作首页](apps/web/workspace/IMPLEMENTATION.md)。

<a id="功能概览"></a>

## 功能概览

- **云模型创作** —— 文生图、图生图、局部重绘与图生视频；实际可用模式由接入的服务和模型决定，不同模型的能力不完全相同。
- **生成参数** —— 选择模型、尺寸与批量数量；其余参数按所选服务能力提供，不要求云模型支持本地采样器、步数或 CFG。
- **任务执行** —— PostgreSQL 事务受理、RabbitMQ 投递、Celery 分阶段执行；SSE 进度、取消、原任务对账与故障恢复
- **结果管理** —— 画布预览、素材管理、下载与删除，生成历史保存在服务端数据库。
- **任务历史** —— 最近任务状态一览(状态点 + 重试入口)
- **持久化** —— PostgreSQL 保存文档、任务、历史与资产元数据，共享媒体卷保存图像/视频；产物收据支持故障后恢复
- **健壮性** —— 参数校验、统一 JSON 错误、队列上限保护、优雅停机、结构化日志
- **云模型接入** —— 通过 Provider 统一适配外部模型 API；保留 Mock 开发演示和可选 ComfyUI 适配。

<a id="快速开始"></a>

## 快速开始

应用只有一套数据库后端。需要 PostgreSQL、RabbitMQ、Redis、OIDC 身份服务与 HTTPS 入口；开发也使用同一套业务架构。Mock 是生成 Provider，不是免数据库的运行模式。

```bash
npm ci
cp -n .env.production.example .env
# 在自己的编辑器中填写 .env：身份、域名、数据库密码、固定路由与凭据文件路径。
# 无付费调用验证可使用 deploy/provider-routes.mock.json 与空的 mock credentials 文件。
docker compose config --quiet
npm run stack:up
```

打开配置的 `AIVERO_PUBLIC_ORIGIN`。登录用户需要通过管理员 CLI 授予工作区成员权限；不会自动创建公开工作区。
完整配置、授权和旧数据导入见[部署手册](docs/runbooks/production-cutover.md)。只创建 `.env` 或选择 Mock 不会自动配置真实 OIDC。

### 源码开发

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
npm run dev -- --env-file /absolute/path/to/dev.env
```

热重载只改变开发体验，仍使用数据库 API 和独立 Worker。宿主开发需提供可访问的独立数据库/Redis、OIDC、路由和共享媒体目录，详见部署手册；Compose 内部服务默认不向宿主公开端口。
`npm start` 使用相同入口但不热重载；`backend.production:create_app` 保留为同一工厂的兼容名称。旧 `AIVERO_API_MODE` 开关已移除，不读取根 `config.json` 选择运行方式。

### 抠图与导出

rembg 直接在 API 的单推理线程运行，无需 7100 服务；首次请求加载 `CUTOUT_MODEL` 对应模型，权重复用但识别结果不缓存。识别蒙版以临时 data URL 返回，确认后生成独立 PNG Asset。取消/超时需等待原生线程完成才释放槽位。
导出进入独立 `exports` Worker，由 FFmpeg 处理时间线快照并通过统一产物协议保存成片。当前新版画布的完整导出界面还未接通。

### 模型配置

云模型、Mock、ComfyUI 都在 `provider-routes.json` 固定 endpoint、实际模型和 `configVersion`；云端只保存 `credentialRef`，真实密钥挂载到生成 Worker。应用不再提供开发模式下的网页改密钥入口。
百炼配置见[云模型接入](docs/百炼生图接入.md)。ComfyUI 需自行部署并在路由指定 `provider=comfyui`、`comfyUrl` 及图片/视频模型；模型能力以当前适配器为准。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 服务与后端状态 |
| GET | `/api/models` | 固定 Provider 路由对应的模型列表 |
| GET | `/api/samplers` | 兼容前端的采样器与调度器选项 |
| POST | `/api/generate` | 提交生成任务(202 + jobId);`initImage` 可选 data URL,`maskImage`(PNG,白 = 重绘)为局部重绘蒙版 |
| GET | `/api/jobs` | 进行中任务;`?all=1` 返回最近任务历史 |
| GET | `/api/jobs/:id` | 单个任务状态 |
| DELETE | `/api/jobs/:id` | 取消任务 |
| GET | `/api/history?limit=100` | 历史记录(新 → 旧) |
| PUT | `/api/images/:id/star` | 设置 / 取消收藏 |
| DELETE | `/api/images/:id` | 从历史索引隐藏图片，保留资产与任务记录 |
| GET | `/api/events` | SSE 实时事件(snapshot / job / image) |

生成请求由已登录的同源浏览器提交，需携带会话 Cookie、CSRF 请求头与稳定的 `requestId`；重试时复用同一个 ID。使用仓库 Mock 路由时，请求体示例：

```json
{
  "requestId": "8c833bb9-5821-4bd1-b10b-f9bd7d29adbc",
  "prompt": "一只宇航员猫，霓虹灯",
  "model": "mock-test",
  "batchCount": 2
}
```

## 项目结构

本项目使用 npm workspaces，前后端仍属于同一个独立 Git 仓库。统一在仓库根目录执行 `npm run dev`、`npm run verify`；也可使用 `npm run dev --workspace @frayune/api` 和 `npm run build --workspace @frayune/web`。迁移记录见 [monorepo 迁移说明](docs/monorepo-migration.md)。

```text
aiVideo/
├── apps/
│   ├── api/
│   │   ├── package.json         # @frayune/api：Python 服务命令入口
│   │   └── backend/             # FastAPI、队列、Provider、存储
│   └── web/                     # @frayune/web：React + TypeScript
│       ├── landing/             # 落地页
│       ├── workspace/           # 创作工作台
│       ├── canvas/              # 正式画布页面、状态与 React/Konva 渲染
│       ├── ui/                  # 前端共享组件
│       └── shared/              # 前端共享逻辑
├── scripts/                     # 开发启动、构建、评测 CLI
├── tests/                       # TypeScript 测试及 backend/ pytest
├── evals/                       # 中文评测用例与规则
├── docs/                        # 技术方案与实施提示词
├── package.json                 # npm workspaces 与统一命令
├── package-lock.json            # 全仓库共用 npm 锁文件
├── pyproject.toml               # Python 检查配置
├── requirements-dev.txt         # Python 开发依赖，使用根目录 .venv
├── .env.production.example      # 统一部署配置模板
├── compose.execution.yaml       # 数据库、消息、调度与 Worker
├── compose.production.yaml      # API、OIDC 与 HTTPS 网关
└── docker-compose.yml           # 合并为统一应用栈
```

**核心数据流**：

```text
浏览器 → FastAPI → PG 事务（Job + Outbox）→ Dispatcher → RabbitMQ → Bridge → Celery Worker
             ↑                                                        ↓
             └── SSE ← PG 状态投影 ← Notify / Redis 唤醒 ← 状态与产物登记 ← StagedProvider
```

生成通过 `providers/staged.py` 的 prepare/submit/poll/cancel 阶段执行；产物先写共享媒体卷与 receipt，再登记 Asset。图片、视频和导出使用独立队列。租约/epoch 防止失联 Worker 晚到覆盖状态，unknown 提交不盲目重试。
历史 FIFO HTTP 实现仅保留于 `evaluation/sandbox.py`，供隔离评测和兼容测试显式调用，不属于可部署应用入口。

## 测试与校验

```bash
npm run verify          # 前端 typecheck / eslint / node:test + 后端 ruff / pytest
PYTHONPATH=apps/api .venv/bin/python tests/integration/compose_smoke.py # 显式隔离部署冒烟
npm run format          # prettier 统一格式
```

CI 同时安装 Node 与 Python 3.11，运行前端类型检查、ESLint、node:test，以及后端 Ruff、pytest。

## Roadmap / TODO

- [x] 核心流程:提示词 → 队列 → 生成 → 画布结果 → 历史持久化
- [x] 采样器 / 调度器选择(选项来自后端能力探测)
- [x] 图生图(参考图上传 + 重绘幅度)
- [x] 局部重绘(Inpainting)—— 前端蒙版画板 + ComfyUI SetLatentNoiseMask / Mock 蒙版合成链路
- [x] 图生视频 Mock 链路(动画占位产物,前端播放支持)
- [x] ComfyUI 图生视频工作流 —— LTX-Video(单 checkpoint)与 Wan / Wan2.2 双 UNet 链路,SaveVideo 出 mp4
- [x] 收藏 / 过滤 / 批量下载 / 任务历史 / 失败重试
- [x] 工程化:verify 门禁、husky、CI、Docker、e2e 冒烟脚本
- [ ] **ComfyUI 真实环境校准** —— 在真实 GPU 上验证图像、视频与重绘工作流(超时与视频模型已可配置)
- [ ] 指令式图片编辑 —— 接入 Qwen-Image-Edit / FLUX.1 Kontext
- [ ] LoRA / ControlNet 支持
- [ ] 云端 GPU 部署指南(AutoDL / RunPod)
- [x] 历史搜索与参数过滤(关键字 / 模型 / 类型 / 收藏)
- [ ] 历史标签管理
- [ ] 多语言界面(i18n)

> 欢迎按下面的贡献流程认领任意条目。

## 参与贡献

1. Fork 并创建特性分支:`git checkout -b feat/your-feature`
2. 提交前运行 `npm run verify` 确保全绿(pre-commit 钩子会自动执行)
3. 提交信息遵循 [Conventional Commits 中文规范](AGENTS.md):`type(模块): 中文描述.`
4. 发起 Pull Request

详见 [AGENTS.md](AGENTS.md) 中的完整开发约定。

## License

[MIT](LICENSE) © 2026 chenGuoFeng


## 运行架构与兼容范围

`backend.app:create_app` 是唯一应用工厂，`python -m backend`、`npm run dev`、`npm start` 与默认 Compose 均指向它。应用状态使用 PostgreSQL；Node 仅用于构建和开发工具，运行镜像使用 Python 3.11、FFmpeg 和非 root 用户。

- `npm run build:web`：TypeScript / React → `.web-build/`，FastAPI 提供静态资源。
- `npm run stack:up` / `stack:down` / `stack:logs`：统一 Compose 应用栈。
- `npm run verify`：类型检查、构建、ESLint、前端测试、Ruff 和后端单测。
- `tests/integration/test_production_api.py`：独立数据库上的当前应用 HTTP/权限/文档/受理/导出契约；运行方式见[集成测试手册](docs/runbooks/integration-testing.md)。

旧 JSON 数据不会在启动时自动迁入数据库，必须停写后备份并执行受控导入。原 Agent、分镜编排、评测 HTTP 页面暂不在当前应用入口提供；离线评测 CLI 和历史契约沙箱仍保留。当前入口和存储统一不等于所有外围功能或生产故障验收完成，详见[实施状态](docs/architecture/durable-execution-status.md)。

画布新增独立的只读 Agent 对话面板，支持 DeepSeek / 百炼、真实图片元信息工具、持久调用记录、停止和刷新恢复。启用需迁移至 `0009` 并配置 Agent 模型路由与 Worker 凭据；默认未配置时不可用。接入及隔离测试步骤见[画布 Agent 手册](docs/runbooks/canvas-agent.md)。

### 时间线与 MP4 导出

时间线数据保存在 Document JSONB；`POST /api/exports` 提交 `{documentId, requestId, timeline}`，返回持久化导出任务。`GET /api/exports/{id}` 查询，`DELETE /api/exports/{id}` 请求取消，`GET /api/exports/{id}/download` 下载成片。FFmpeg 在独立 Worker 内运行，具备进程组回收和产物恢复；新版画布尚未装配完整的 TimelineExport flow。

## 创作 Harness：生成后反思（Reflection）

`apps/server` 的 Host 在生图结果返回后、计划步骤完成前执行 `finalizeGeneration`：硬检查（空文件、分辨率、比例）→ Reviewer port 审核 → 接受、自动重试或超限交回用户。默认使用确定性 stub，不调用付费 VLM；真实多模态适配器可通过 `ports.reviewer` 注入，读取作品 URL 与任务类型 rubric。Mock 产物提供尺寸与字节元数据；接入真实生成 Provider 时应提供实测元数据及需求尺寸/比例，不能用 stub 判定代替真实画质审核。

| 配置（createApplication） | 环境变量 | 默认值 |
| --- | --- | --- |
| `reviewEnabled` | `REVIEW_ENABLED` | `true` |
| `reviewThreshold` | `REVIEW_THRESHOLD` | `0.7` |
| `maxReviewRetry` | `MAX_REVIEW_RETRY` | `2`（首轮之外再试两次） |
| `retryCredits` | `RETRY_CREDITS` | `true` |
| `rubric.image` / `rubric.video` | 通过 composition root 注入 | 图片：相关性/明显残缺/比例；视频：主题/连贯性 |

必须硬检查全部通过、评分达标且 rubric 关键项通过才能接受。Trace 保持 `version:"1"`，生成工具记录 `evidence.checks`、`evidence.review`、`reviewHistory`、`reviewAttempts`（自动重试次数）和 `reviewOutcome`。重试沿用工具调用/计划步骤身份，使用 `toolCallId:attempt` 新幂等键；首轮保留原 `toolCallId` 键以兼容 S2/S3。审核关闭时不调用 reviewer，也不新增审核证据。

执行状态保存当前 attempt，确认批准只覆盖当时的尝试；重试重新估算积分，超过阈值仍触发 interrupt。默认每次生成都计积分；`retryCredits=false` 时仅首轮计积分。用户只看到一行重试状态；达到上限后作品卡标记「未通过质量检查」，助手说明审核 issues，并提示调整需求。默认 stub 的超限路径结束失败步骤；不调用付费 Planner。取消和跨轮继续保留 attempt，取消中的队列任务重新分配执行 ID。

L7 题库见 `evals/datasets/agent-flow-l7-reflection.jsonl`，工作台可选 L7 独立查看和运行。2026-10-10 验收：L0～L5 的 26 题与 L7 的 4 题共 30/30 通过；F4 忽略低分/失败关键项后 L7 变红（`unexpected_retry`），撤销注入后恢复绿色。复现：

```bash
python3 scripts/eval.py flow --suite L7
python3 scripts/verify_agent_eval_sensitivity.py --fault f4 --store /tmp/aivideo-reflection-f4-new
npm run test --workspace @frayune/creation-server
npm run verify
```

完整校验的实际结果与证据说明见 [评测 README](evals/README.md#reflectionl7-验收)。

### 可插拔 Reviewer（stub / real）

默认 `REVIEWER_MODE=stub`，审核证据的 `reviewer` 为 `stub`；只有显式 `real` 才调用真实多模态 API。未知 mode 回退 stub。`apps/server/src/ports/index.ts` 定义 `ReviewInput` / `ReviewResult`：包含作品 URL/本地路径/夹具标识、prompt、taskType、rubricId、attempt、conversationId/turnId/toolCallId 上下文。输出继续写入同一 `evidence.review`，Trace 版本及前端文案不变。

真实实现先支持图片，使用与现有 Python VlmJudge 相同的 OpenAI 兼容 `chat/completions` 协议。配置示例（密钥仅通过当前进程环境或部署 secret 注入）：

```bash
export REVIEWER_MODE=real
export REAL_REVIEWER_API='https://your-vlm.example/v1/chat/completions'
export REAL_REVIEWER_MODEL='支持视觉的模型名'
# REAL_REVIEWER_API_KEY 由环境/secret 注入；不写入源码、Trace 或提交到 Git。
export REAL_REVIEWER_TIMEOUT_MS=15000
export REVIEW_RUBRIC='{"image":["相关性","明显残缺","比例"]}'
npm run dev:creation-server
```

沿用 `REVIEW_THRESHOLD=0.7`、`MAX_REVIEW_RETRY=2`。`REVIEW_RUBRIC` 为按 image/video 配置的 JSON 字符串数组对象。程序化配置为 `createApplication({ reviewer: { mode: 'real', real: { api, model, timeoutMs } } })`；也可继续通过 `ports.reviewer` 注入自定义实现。API Key 由工厂读取 `REAL_REVIEWER_API_KEY`，不进入可序列化应用配置。主进程从 env 选择模式；离线 harness 的 composition root 默认始终为 stub，不受开发机 `REVIEWER_MODE` 影响。

超时默认 15 秒，可设置 1～15000ms，覆盖请求、读取响应和解析过程。超时、空响应、坏 JSON、协议缺项或 HTTP 错误都得到零分/失败关键项及中文 issues，交给现有有限 retry → escalate；适配器不自行重试。用户停止操作传播取消信号。重试重新估算积分和 interrupt，未获得确认不继续生成。`reviewer` 由配置模型名写入、attempt 由本地上下文写入，模型不能伪造这两个字段。

真实模式需生成 Provider 提供模型可访问的 PNG/JPEG/WebP/GIF 图片 URL 或 `metadata.localPath`（本地图片转 data URL，上限 20MB）；URL、本地文件不能仅用 fixture id 替代。默认 mock 生图仍返回本机 SVG，开启审核不会自动切换生成 Provider，不能把此 SVG 当作真实图片审核的有效输入。真实视频审核暂未实现，会返回可读失败原因并走重试上限。

可选真实冒烟使用仓库茶饮 JPEG，通过完整 Host 流程检查 accepted 和真实模型 Trace：

```bash
# 配齐 REAL_REVIEWER_API / MODEL / API_KEY 后显式执行，可能产生模型费用。
REAL_REVIEWER_SMOKE=1 node --import tsx --test apps/server/tests/real-reviewer.smoke.test.ts
```

默认 CI 即使有 key 也不执行真实请求；未显式 opt in 或无 key/模型/API 时测试 skip（`MARK_SKIP_IF_NO_KEY`）。无付费模型调用的验收结果见 [评测 README](evals/README.md#可插拔-reviewer-验收)。

### Host 意图与视频路由（P0）

完整设计见 [TECH_INTENT_VIDEO_ROUTING.md（v2）](TECH_INTENT_VIDEO_ROUTING.md)。
`apps/server` 的 Host 在生成前与续跑时执行意图、模板和最终视频产物门禁；视频工具未注册或未启用返回 `video_unavailable`，不会转成生图。

默认配置：`INTENT_MODE=rules_only`、`PLAN_VALIDATOR=enforce`、`IMAGE_VIDEO_STRATEGY=multiframe_first`、`DEFAULT_FRAME_COUNT=4`、`MAX_FRAME_COUNT=8`。
联调结构化分类可用 `INTENT_MODE=rules_plus_llm`，通过现有 LLM port 的 `intent_result` JSON schema 工具输出；生产应配置真实 provider。
`VIDEO_TOOL_ENABLED`、`MULTIFRAME_ENABLED`、`DIRECT_I2V_ENABLED` 默认均为 `false`，只在对应工具真实接入后开启；P0 测试使用显式注入的 stub，未上线 P1 真多帧链。

词表在 `config/intent_lexicon.yaml`（JSON 兼容 YAML 1.2）。参考图通过 `imageRefs` 或 `canvas.assetUrls` 输入；外部 URL 仅允许 `REFERENCE_URL_ALLOWED_HOSTS` 中的精确域名（HTTPS），默认只允许本地开发域名和 `asset:` 内部资产引用。白名单检查发生在 LLM 与工具调用之前。
多参考和低置信度会挂起为 `awaiting_user` / `interrupt_kind=clarify`；`resume_interrupt` 可携带 `clarification: {imageRefs: [选定参考], goal: "image"|"video"}`，恢复后重新校验。直接确认多参考卡会按卡片提议选择第一张。确认有效期为 24 小时。

多帧计划一次确认总价；P0 帧失败采用 `abort_all`，保留已完成步骤的实际消耗与可续跑帧，不实现部分退款。节点 CAS 写回、帧并行、自有桶与人审台仍按 v2 分期。
验证：`npm run verify`、`npm test --workspace @frayune/creation-server`。

### 持久化多 Agent Host

生产 Host 已接入统一 PostgreSQL / Outbox / RabbitMQ / Celery 的媒体 DAG、积分流水、节点租约、样片与正片流程。默认禁用，配置与隔离验收见 [多 Agent 运行手册](docs/runbooks/multi-agent-rollout.md)。`apps/server` 的内存 Host 仅用于开发评测，不能作为生产入口。


### 主线与本地入口

日常开发统一使用本仓库 `aiVideo` 的 `https://localhost:8443`（`npm run dev:stack`）。
画布继续使用 AgentPanel/AgentRun 与 PostgreSQL/Celery 媒体链路；不再启动独立的 8445/8798 Host。
模块移植与退役说明见 [8443 统一记录](docs/runbooks/8443-consolidation.md)。

### 分层意图与顺位多步（8443 Host）

实现依据：[分层意图 v1.2](docs/intent/TECH_LAYERED_INTENT.md)、[RAG 与闭环验收](docs/intent/HANDOFF_INTENT_RAG_FEEDBACK_LOOP.md)、[反馈闭环](docs/intent/TECH_INTENT_FEEDBACK_LOOP.md)、[顺位交接](docs/intent/HANDOFF_INTENT_P2_SEQUENTIAL.md)、[案例册](docs/intent/CASES_INTENT_SEQUENTIAL.md)。

- `INTENT_RAG=false`：默认保留原 Host 路径；`true` 启用结构门、门闩、批准例句向量检索、槽位与绑定原参考快照的澄清。新路径要求 `context`，缺失返回 `CONTEXT_REQUIRED`。
- `INTENT_MULTI_STEP=false`：默认关闭，且依赖 `INTENT_RAG=true`。顺位句要求澄清或拆成两次发送。开启后最多两步，仅允许 `image_edit → image_to_multiframe`（含 matting）或 `text_to_image → image_to_multiframe`。强「先…再…」可直接排计划，其它顺位需确认「一次做完」。同 L2 的多个约束仍是单步。
- `INTENT_DIRECT_I2V=false`：默认多帧视频；仅显式开启并明确要求一步 I2V 才走直接图生视频。
- `INTENT_RAG_SHADOW=false`：开启而 RAG 主开关关闭时只记录新决策，仍由旧路径执行。日志只含原话 hash 和槽位摘要；`AgentRun.execution.intentTrace` 保存版本、topK、门闩、降级、语义步骤与 `step_results`。
- 后步关键帧只使用前步产物，依赖失败不调度后步；`step_fail_index` 从 0 开始。视频能力关闭或规格不支持明确报错，绝不改成静态图片。积分、安全与限流继续使用原 admission/Worker 错误码。

向量侧车需在本机或内网单独部署，提供 `POST /v1/embeddings`：请求 `{ "model": "固定模型版本", "input": ["文本"] }`，返回 `{ "model": "同一版本", "data": [{ "embedding": [0.1, 0.2] }] }`。例如将内网的 sentence-transformers 服务适配为该协议，启动后设置：

```dotenv
INTENT_RAG=false
INTENT_MULTI_STEP=false
INTENT_RAG_SHADOW=false
INTENT_EMBED_URL=http://127.0.0.1:8091/v1/embeddings
INTENT_EMBED_MODEL_ID=部署固定的多语模型版本
INTENT_EXEMPLARS=/absolute/path/exemplars-0.2.1.yaml
INTENT_INDEX=/absolute/path/exemplars-0.2.1.index.json
```

未配置索引、模型版本不一致或侧车超过 0.8 秒预算时记录 `embed_degraded=true`，除确定性门闩外保守澄清。阈值初值 strong=0.82、跨 L2 margin=0.12、min=0.55，须用独立标注集标定后才能认定生产召回达标。

随代码提供的 `intent/exemplars.yaml` 是只读文档 v0.2.0 的**待审副本**，没有自动批准任何例句，初始线上索引为空。人工审批前不能把固定测试夹具的通过率当作实际 embedding 准确率。批准后先 shadow，核对高成本误判与拒识分母，再灰度 `INTENT_RAG=true`；P0 验收通过后才开启多步。

候选在现有 `AgentRun.execution.intentFeedback` 中事务持久化，包含来源、原文、L2、原 request_id、快照、时间和用户动作。多步带 `sequential_candidate`，不能成为单步金牌。成功任务候选需至少稳定 60 秒，撤销或取消状态不导出；任何候选都不自动入库。显式指定候选来源数据库后运行：

```bash
# INTENT_FEEDBACK_DATABASE_URL 显式设置为获授权的候选数据库连接。
.venv/bin/python scripts/intent_exemplars.py export --output /absolute/path/candidates.jsonl
# heldout.json 为隔离评测原话 JSON 数组；包含固定回归与盲测，禁止吸收入库。
.venv/bin/python scripts/intent_exemplars.py wash --candidates /absolute/path/candidates.jsonl --heldout /absolute/path/heldout.json --output /absolute/path/ready.jsonl --rejected /absolute/path/rejected.jsonl
.venv/bin/python scripts/intent_exemplars.py promote --library /absolute/path/exemplars-0.2.0.yaml --candidates /absolute/path/ready.jsonl --heldout /absolute/path/heldout.json --approve 候选ID --approved-by 审校人 --output /absolute/path/exemplars-0.2.1.yaml
.venv/bin/python scripts/intent_exemplars.py reindex --library /absolute/path/exemplars-0.2.1.yaml --model-id 固定模型版本 --output /absolute/path/exemplars-0.2.1.index.json
```

所有脚本输出新文件，不覆盖在线版本。初始化种子库须人工逐条审校，通过 `scripts/intent_exemplars.py approve-seeds --library 待审库 --heldout 隔离集 --approve 明确批准的ID --approved-by 审校人 --model-id 固定版本 --output 新版本.yaml` 执行清洗、泄漏检查和 Approve；该命令不接收“全部批准”快捷参数。歧义条目保留 pending，评测专用条目标记 `evaluation_only: true`。回滚时先关闭 `INTENT_MULTI_STEP`，需要时关闭 `INTENT_RAG`；或同时切回旧 `INTENT_EXEMPLARS`、`INTENT_INDEX` 和对应模型版本并重启 Host。旧版本 YAML 与索引必须成对保留；摘要或模型不匹配会拒绝加载。

验证：`npm run verify` 包含 `test_intent_pipeline.py`（E/S 安全门）、`test_intent_feedback.py`（A 门）、前端指纹与分步测试；独立且迁移后的 PostgreSQL/Redis 上执行 `tests/integration/test_intent_host.py` 与 `tests/integration/test_production_api.py`，分别验证实际依赖入队和 HTTP/存储回归。没有批准库、固定侧车与标注基线时不宣称生产 RAG 标定或 O04 已完成。

可选本机侧车启动（模型目录须预先下载并固定快照，应用不会替你下载或调用云 embedding）：

```bash
.venv/bin/python -m pip install -r requirements-intent.txt
INTENT_LOCAL_MODEL_PATH=/absolute/path/model-snapshot INTENT_EMBED_MODEL_ID=固定快照版本 .venv/bin/uvicorn scripts.intent_embed_server:create_app --factory --host 127.0.0.1 --port 8091
```

固定安全集对照命令：`.venv/bin/python scripts/eval_intent.py --output /absolute/path/shadow-report.json`；当前记录在 [safety-baseline.json](evals/intent/safety-baseline.json)，明确保留误判分母与 `production_calibrated=false`。CI 单测执行同一安全集，S07 另由隔离数据库的失败注入覆盖。它证明门闩/降级安全性，真实模型召回、阈值及灰度效果仍须批准库和实际侧车评测。

清洗器还内置案例册和固定安全集的隔离原话清单（`intent/heldout.json`）；即便操作者漏传某条固定回归，也不能通过 Approve 混入线上索引。额外盲测继续通过 `--heldout` 补充。生产 Compose 用 `INTENT_ARTIFACTS_DIR` 将批准版本只读挂载到 `/run/config/intent`；环境变量须填写容器内路径。阈值可通过 `INTENT_T_STRONG` / `INTENT_T_MARGIN` / `INTENT_T_MIN` 配置，变更后重启并重新评测。

58 条产品案例的机器可读副本在 [sequential-cases.json](evals/intent/sequential-cases.json)，保持案例编号与 P0/P2 期望原文，供批准库后的完整语义评测使用；它不作为训练例句。
