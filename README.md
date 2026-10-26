<div align="center">

<a href="https://weilengdezaoshang.github.io/aiVideo/">
  <img src="docs/designs/github-pages-2026-09-22/jimeng-v2/readme-cover.png" width="1200" alt="FRAYUNE 帧屿集：让灵感，成为下一帧。" />
</a>

# 帧屿集 FRAYUNE

让灵感，成为下一帧。

开源 AI 视觉创作平台，将图像与视频生成、画布编辑、Agent 编排和素材管理放在同一个工作空间。

[项目主页](https://weilengdezaoshang.github.io/aiVideo/) · [快速开始](#快速开始) · [部署](#部署) · [架构](#架构) · [评测](#评测) · [开发约定](AGENTS.md)

[![CI](https://github.com/weilengdezaoshang/aiVideo/actions/workflows/ci.yml/badge.svg)](https://github.com/weilengdezaoshang/aiVideo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

React + TypeScript + Konva · Python + FastAPI · PostgreSQL + RabbitMQ + Redis + Celery

</div>

## 项目能做什么

| 能力 | 当前实现与使用条件 |
| --- | --- |
| 图像与视频生成 | 通过 Provider 接入 Cloud、ComfyUI 或 Mock；文生图、参考图编辑、局部重绘与视频能力取决于固定路由和所选模型。 |
| 创作画布 | React / react-konva 画布，支持素材编排、节点生成、参考图、文档保存及结果恢复。工作台入口为 `/workspace`，画布入口为 `/canvas`。 |
| 抠图 | API 内使用 rembg 识别前景，查看和调整蒙版后提交 PNG 资产；首次识别可能下载并加载模型权重。 |
| Agent 编排 | Python supervisor 负责计划与推进，结合计划审核、自检、工具执行、用户确认和会话中断；可用范围受模型路由、工具目录和预算配置约束。 |
| 技能与批量任务 | 代码包含电商、海报、分镜等官方技能，以及批量展开、重试和可编辑分层相关执行逻辑。具体启用条件见对应手册。 |
| 持久化与恢复 | PostgreSQL 保存文档、Run、Task、历史和资产元数据；共享媒体卷保存产物；支持 SSE 状态更新、取消、对账及故障恢复。 |
| 时间线导出 | 画布集成时间线剪辑、音轨与导出控制；后端按时间线快照创建任务，由独立 FFmpeg Worker 导出 MP4。 |
| 评测 | 独立本地评测工作台、离线规则门禁、流程评测、录制回放和隔离数据库可靠性回归。 |

[GitHub Pages](https://weilengdezaoshang.github.io/aiVideo/) 是静态项目介绍页。创作平台需要自行部署。Mock 用于验证任务与产物流转，结果不能代表真实模型质量。

## 快速开始

### 本地开发与 Mock 体验

推荐使用 **Node.js 24、Python 3.11+、Docker Compose v2**，并准备 Git 与 OpenSSL。Node 24 与仓库 CI、Docker 构建保持一致；根包声明的最低 Node 版本为 20，独立 Node Host 要求 22+。

```bash
git clone https://github.com/weilengdezaoshang/aiVideo.git
cd aiVideo

npm ci
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt

npm run dev:stack
```

启动后打开 **https://localhost:8443/**。登录经本地 OIDC fixture 自动完成，使用 `dev-user`；脚本会为该用户授予开发工作区管理员权限，并幂等初始化 Mock 测试积分。

`dev:stack` 会构建当前代码，启动 PostgreSQL、RabbitMQ、Redis 和 Worker 容器，在宿主机启动 TLS API 与本地 OIDC。默认使用 Mock 生成路由和独立开发数据，不需要付费模型凭据。

| 服务 | 本地端口 |
| --- | --- |
| API / 工作台 / 画布 | `https://localhost:8443` |
| 开发 OIDC fixture | `https://localhost:8444` |
| PostgreSQL | `127.0.0.1:55437` |
| RabbitMQ | `127.0.0.1:5677` |
| Redis | `127.0.0.1:6387` |

证书优先使用已安装并信任的 mkcert，否则回退为 OpenSSL 自签证书。自签证书需要在浏览器中信任；配置与排障见[本地开发手册](docs/runbooks/dev-environment.md)。

```bash
# 停止开发进程与容器，保留数据
npm run dev:stack:down
```

配置、证书、日志和媒体位于 `data/dev/`。API 配置由 `deploy/dev/api.env.example` 生成到 `data/dev/api.env`。再次启动会重新构建并刷新进程；后端代码变更后需重启，前端变更可先执行 `npm run build:web` 再刷新页面。

### 使用已有基础设施开发

若已准备数据库、Redis、OIDC、模型路由、共享媒体目录及独立 Worker，可通过显式配置启动热重载 API：

```bash
npm run dev -- --env-file /absolute/path/to/dev.env
```

`npm start` 使用同一应用入口，关闭热重载。所有应用启动方式都使用 PostgreSQL / Celery 业务架构，`--reload` 仅控制代码监听；旧 `AIVERO_API_MODE` 已移除，根目录 `config.json` 不用于选择运行后端。

## 部署

正式部署使用统一 Compose 应用栈和真实 OIDC 身份服务。

```bash
cp -n .env.production.example .env
# 编辑 .env，并准备路由与凭据文件
docker compose config --quiet
npm run stack:up
```

部署前至少需要准备：

- **基础设施与身份**：数据库、消息队列密码，HTTPS 域名，OIDC issuer / client ID / client secret，以及独立的 session secret。
- **生成路由**：`AIVERO_ROUTES_FILE` 指向固定 Provider endpoint、模型和版本的配置文件。
- **Worker 凭据**：`AIVERO_CREDENTIALS_FILE` 指向凭据注册表，由 Worker 解析 `credentialRef`。密钥与部署 `.env` 不应提交到仓库。
- **共享媒体与执行配置**：API 和 Worker 共用媒体卷；Core 执行需要稳定的 `AIVERO_EXECUTION_TICKET_KEY`。
- **可选 Agent / Host**：Agent 路由、persona 和 Host 工具目录各自配置。生产模板默认禁用 Agent 路由与 Host 目录，启用前需配置模型、工具绑定和积分。

只验证部署链路时，可使用 `deploy/provider-routes.mock.json` 和 `deploy/provider-credentials.mock.json`；仍需完整的数据库、OIDC 和 HTTPS 配置。

登录用户需要显式加入工作区。管理员通过 `backend.tools.production_admin grant-member` 授权，操作会记录审计事件。完整配置、授权、数据迁移及回滚步骤见[生产部署手册](docs/runbooks/production-cutover.md)。

```bash
npm run stack:logs
npm run stack:down
```

Docker 使用 Node 阶段构建前端，运行阶段为 Python 3.11、FFmpeg 和非 root 用户。应用不会在启动时自动导入旧 JSON 数据。

### 接入真实模型

图像 / 视频生成和 Agent 对话使用各自的路由配置；仅配置其中一个不会自动启用另一个。未配置对话模型路由时，Python supervisor 使用规则 planner；LLM 规划需显式配置对应路由。

- [百炼生图接入](docs/百炼生图接入.md)：生成 Provider 路由、模型与 Worker 凭据。
- [只读画布 Agent（兼容面板）配置](docs/runbooks/canvas-agent.md)：对话模型、persona 与调用恢复。
- [Python supervisor](docs/runbooks/core-supervisor.md)：规划、审核、执行与预算边界。
- [多 Agent 上线手册](docs/runbooks/multi-agent-rollout.md)：部署检查与启用范围。

真实模型调用会产生对应服务费用。取消请求与服务商实际停止、最终积分结算是不同阶段；已提交且结果未知的请求需要对账，平台不会将其视为可以直接免费重提的任务。

## 架构

### 三个应用目录的职责

| 目录 | 职责 | 生产部署关系 |
| --- | --- | --- |
| `apps/api` | FastAPI 应用、身份与权限、文档与资产、Python supervisor、事件处理和 Celery 执行。 | 正式应用入口。`backend.app:create_app` 委托 `backend.production:create_app`。 |
| `apps/web` | React / TypeScript 工作台、Konva 画布与评测前端。 | 构建到 `.web-build/`，由 FastAPI 提供静态资源。 |
| `apps/server` | Hono / TypeScript 创作 Host，包含内存队列、Mock 工具、Trace 和流程评测入口。 | 独立开发与评测实现；不进入生产运行镜像，`NODE_ENV=production` 时拒绝启动。 |

生产 supervisor 运行在 Python 中。Node Host 有自己的规划、审核与执行实现，其流程评测结果不能直接证明生产 Python supervisor 的行为。

```text
浏览器 / React 画布
        │ REST + SSE / OIDC
        ▼
FastAPI ── PostgreSQL：文档、资产、Run / Task、事件与调用记录
        │
        ├── Job + Outbox → Dispatcher → RabbitMQ → Bridge → Celery Worker
        │                                                   │
        └── Core 事件处理 → Python supervisor → 审核 / 准入 → 工具执行
                                                            │
                                   Provider / FFmpeg / 本地工具
                                                            │
                                共享媒体卷 + 产物收据 + 数据库登记
                                                            │
                                   Redis 唤醒 / 状态投影 → SSE
```

生成 Provider 采用 `prepare / submit / poll / cancel` 阶段协议。图片、视频、导出与 Agent 使用独立队列；执行层通过租约、幂等记录与产物收据恢复任务。历史 HTTP / JSON 夹具保留在 `evaluation/sandbox.py` 等模块中，供显式隔离评测使用。

### 代码导航

```text
apps/api/backend/
├── app.py / production.py      # 应用工厂、REST、SSE、OIDC
├── core/                      # Run / Task、会话、supervisor、审核、技能与执行
├── agent/                     # 对话路由、Host 媒体工具与适配
├── services/                  # 文档、事务受理、生成与导出
├── infrastructure/            # PostgreSQL、Outbox、消息与产物收据
├── providers/                 # Mock / Cloud / ComfyUI 阶段适配
├── workers/                   # Celery 与独立后台进程
└── evaluation/                # 评测、回放、规则和可靠性夹具
apps/web/                      # 工作台、画布、共享 UI、评测页面
apps/server/                   # 独立 Node Host 与 Trace 流程评测
scripts/                       # 启动、构建、管理与评测 CLI
evals/                         # 用例、规则及评测说明
deploy/                        # 部署配置模板
tests/                         # 前端、后端与隔离集成测试
```

### 主要接口

| 接口 | 用途 |
| --- | --- |
| `/auth/login`、`/api/session` | OIDC 登录与当前会话。 |
| `/api/documents`、`/api/assets` | 画布文档与资产管理。 |
| `/api/generate`、`/api/jobs` | 生成受理、状态、取消与对账。 |
| `/api/history`、`/api/events` | 生成历史与 SSE 事件。 |
| `/api/core/runs` | Python supervisor Run 创建与进度。 |
| `/api/exports` | 时间线导出任务与成片下载。 |

多数业务接口需要登录身份与工作区权限；浏览器写请求还需 CSRF 校验。生成重试应复用稳定 `requestId`。接口参数与权限以当前源码为准，详细执行协议见 [Core 执行手册](docs/runbooks/core-execution.md)。

## 评测

### 本地评测工作台

```bash
npm run dev:evaluation
```

打开 **http://127.0.0.1:8788/**。它是独立 FastAPI 应用，证据数据写入 `data/eval-web/`，不使用生产应用的登录和工作区权限；默认仅监听回环地址。

### 离线与隔离回归

```bash
# Node Host 流程评测，默认不调用付费模型
.venv/bin/python scripts/eval.py flow --suite agent_flow --store /tmp/frayune-flow-eval

# Python planner 离线门禁，使用 Mock 结果验证规则与管线
PYTHONPATH=apps/api .venv/bin/python -m backend.evaluation.planner_gate

# 独立容器内的可靠性回归，使用假接口
PYTHONPATH=apps/api .venv/bin/python -m backend.evaluation.reliability.cli --output /tmp/frayune-reliability
```

评测工作台通过独立脚本启动。生产应用未挂载 `/api/evals/v1`，不要将 `/internal/runs` 当作已部署的评测工作台。

流程评测会由 Python 调用 `apps/server/src/eval/run-flow-case.ts`，读取 SSE、Trace 与 observation 后判分。Python supervisor 的取消、扣费、恢复和会话行为另由数据库集成测试验证。

评测命令并非全部离线：显式选择真实生成 Provider、真实 Reviewer 或 LLM planner 的专项可能调用付费服务。模式与参数详见[评测说明](evals/README.md)、[LLM planner 评测](docs/runbooks/llm-planner-evaluation.md)和[可靠性手册](docs/runbooks/stage1-reliability.md)。

## 开发与验证

统一在仓库根目录执行：

```bash
npm run typecheck
npm run build:web
npm run verify
```

`verify` 包含 TypeScript 类型检查、前端构建、ESLint、前端测试、Node Host 检查与测试，以及 Python Ruff / pytest。数据库集成测试需显式指定隔离测试库与 Redis 并先执行迁移，见[集成测试手册](docs/runbooks/integration-testing.md)。

CI 还运行 planner、自检、技能离线门禁和容器可靠性回归。`npm run verify` 通过不等于所有部署与数据库集成检查已经完成。

开发约定和中文提交格式见 [AGENTS.md](AGENTS.md)。欢迎通过 Issue 报告问题，或通过 Pull Request 提交改进。

## 文档索引

- [开发环境](docs/runbooks/dev-environment.md) · [正式部署](docs/runbooks/production-cutover.md)
- [会话与中断](docs/runbooks/session-conversations.md) · [计划审核](docs/runbooks/core-review.md) · [计划自检](docs/runbooks/plan-selfcheck-evaluation.md)
- [事件与恢复](docs/runbooks/core-events.md) · [工具执行](docs/runbooks/core-execution.md)
- [评测体系](evals/README.md) · [项目主页维护](website/README.md) · [品牌资源](docs/brand/frayune/README.md)

## License

[MIT](LICENSE) © 2026 chenGuoFeng
