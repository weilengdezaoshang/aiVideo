# 开发约定

本仓库(aiVideo)是独立 npm workspaces monorepo（apps/api、apps/web）:SwarmUI 风格的 AI 生成应用。Python 3.11+ + FastAPI 后端,
TypeScript 前端(工作台、落地页与画布为 React + TSX，画布使用 react-konva)。应用统一使用 PostgreSQL / RabbitMQ / Redis / Celery，Provider 支持 Mock、Cloud 与 ComfyUI。

## 前端语言规范

- 前端业务代码必须使用 TypeScript (`.ts` / `.tsx`) 编写;新功能、新模块和新增测试不得使用 JavaScript (`.js`)。
- 前端如再出现 `.js` 业务代码，应在同一任务中迁移到 TypeScript；不得继续扩大 JavaScript 代码范围。
- DOM、画布和 Provider 调用的类型必须显式建模，禁止用 `any` 规避类型检查；确需第三方或遗留边界适配时，将 `any` 限制在最小边界并写明原因。
- 新增前端 TypeScript 代码必须纳入现有 `tsconfig`、typecheck 和 lint 流程，提交前运行 `npm run verify`。

## 常用命令

- `npm run dev:stack` / `npm run dev:stack:down` — 一键本地开发环境(mock Provider + 本地 OIDC,API 8443 TLS);详见 `docs/runbooks/dev-environment.md`
- `npm run dev -- --env-file /absolute/path/to/dev.env` — 启动同一数据库 API 的 Uvicorn 热重载；需显式基础设施、OIDC、路由与共享媒体配置
- `npm run stack:up` — 启动统一 Compose 应用栈；先填写 `.env.production.example` 对应配置(生产环境,与 dev:stack 分离)
- `npm run verify` — typecheck + eslint + 测试,提交前必跑(pre-commit 会自动执行 typecheck)
- `npm run format` / `npm run lint:fix` — 格式化 / lint 自动修复
- `LOG_LEVEL=debug npm run dev` — 开启请求级访问日志

## 提交规范

所有提交必须是 `type(模块): 中文描述.`(结尾英文句点),由 commit-msg 钩子强制校验:

- 类型:`feat` `fix` `docs` `refactor` `test` `perf` `style` `build` `ci` `chore` `revert`
- 模块:`server`(API/服务端)、`web`(前端)、`provider`(生成后端)、`queue`(任务调度)、
  `store`(持久化)、`config`(配置)、`project`(仓库级配置与文档)
- 示例:`feat(provider): 新增 ComfyUI 图生图支持.` / `fix(queue): 修复取消任务未清理参考图的问题.`

一个提交只处理一个完整目标;不提交密钥与 `data/` 运行时产物。

## 架构速览

```
apps/api/backend/app.py                  # 唯一应用工厂，委托数据库 API
apps/api/backend/production.py           # REST + SSE + OIDC + PG 应用服务
apps/api/backend/services/               # 事务受理、文档、阶段执行与恢复
apps/api/backend/workers/                # Celery Worker + 四类独立 daemon
apps/api/backend/infrastructure/         # PostgreSQL/Outbox/RabbitMQ/Redis/产物收据
apps/api/backend/providers/              # Mock / Cloud / ComfyUI 分阶段适配
apps/api/backend/evaluation/sandbox.py   # 显式隔离的历史契约与离线评测夹具
apps/web/                               # React + TypeScript + react-konva
```

所有应用启动方式使用同一 PostgreSQL/Celery 架构；`--reload` 只控制代码监听，不切换业务模式。
`backend.production:create_app` 是同一工厂的兼容导入，`AIVERO_API_MODE` 已移除。
新增生成后端接入 `providers/staged.py` 的阶段协议；模型与 endpoint 在部署路由固定，真实凭据由 Worker
通过 credentialRef 解析。应用不读取根 `config.json` 或 `SWARMUI_PROVIDER` 选择运行后端。

Python 后端使用 `.venv`，依赖见 `requirements-dev.txt`；`npm run verify` 同时运行前后端检查。
涉及当前 HTTP/存储契约变更还应执行 `tests/integration/test_production_api.py`，显式配置独立
`AIVERO_DURABLE_TEST_DB` / `AIVERO_DURABLE_TEST_REDIS`，先执行迁移；不可连接生产数据或使用真实付费凭据。
历史 JSON/FIFO 仅供 `evaluation/sandbox.py` 与 `sandbox_jobs.py` 回归使用，必须显式传入独立目录，
不得作为应用入口或替代当前数据库集成测试。旧数据由离线导入工具处理，禁止启动时自动改写。

## 抠图与前端构建

- `npm run build:web` 使用 `tsconfig.web.json` 编译画布 TS，并通过 `scripts/build-react.ts` 打包 React 工作台与落地页到 `.web-build/`；FastAPI 在原 `.js` 路径提供结果，不提交产物或手写同名 JS。
- 前端测试基于 `node --test` + tsx；DOM/组件测试经 `tests/dom.ts` 安装 jsdom 全局并配 Testing Library,纯逻辑测试保持无 DOM。
- `npm run dev` 自动编译并监听前端 TS；Docker 在 Node 阶段构建前端，运行阶段只有 Python。
- `apps/api/backend/cutout.py` 在后端进程的单推理线程中直接调用 rembg，不依赖 `CUTOUT_SERVICE_URL` 或 7100 端口；`CUTOUT_MODEL` 配置模型，首次请求加载。
- 识别操作使用 operationId、AbortController 和源对象身份检查；切换选择立即取消，迟到结果不得改写新会话。
- 取消请求不能强杀原生线程，线程实际完成前不得释放并发槽位。
- 不缓存 mask 或持久化笔画草稿；退出蒙版后再次抠图必须重新识别，禁止恢复旧选区。模型权重复用不等于图片识别结果缓存。已提交的抠图合成独立于识别会话。
