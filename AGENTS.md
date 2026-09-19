# 开发约定

本仓库(aiVideo)是独立 npm workspaces monorepo（apps/api、apps/web）:SwarmUI 风格的 AI 生成应用。Python 3.11+ + FastAPI 后端,
TypeScript 前端(工作台与落地页为 React + TSX,画布为原生 HTML/CSS + TypeScript,前端已无 .js 业务代码),Provider 抽象支持 Mock 与 ComfyUI 后端。

## 前端语言规范

- 前端业务代码必须使用 TypeScript (`.ts` / `.tsx`) 编写;新功能、新模块和新增测试不得使用 JavaScript (`.js`)。
- 前端如再出现 `.js` 业务代码，应在同一任务中迁移到 TypeScript；不得继续扩大 JavaScript 代码范围。
- DOM、画布和 Provider 调用的类型必须显式建模，禁止用 `any` 规避类型检查；确需第三方或遗留边界适配时，将 `any` 限制在最小边界并写明原因。
- 新增前端 TypeScript 代码必须纳入现有 `tsconfig`、typecheck 和 lint 流程，提交前运行 `npm run verify`。

## 常用命令

- `npm run dev` — 启动开发服务(默认 http://127.0.0.1:7801,Uvicorn 热重载)
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
apps/api/backend/app.py                  # FastAPI 入口:REST + SSE + 静态资源 + 生命周期
apps/api/backend/jobs.py                  # FIFO 任务队列:调度、进度、取消、事件
apps/api/backend/providers/              # Provider 抽象 + Mock / 云端 / ComfyUI 实现
apps/api/backend/storage.py               # 文档、素材与历史的磁盘持久化
apps/web/                             # 前端(HTML/CSS + TypeScript;工作台/落地页为 React + TSX,画布为原生 DOM + Konva TS)
```

新增生成后端:实现 `apps/api/backend/providers/base.py` 的 `Provider` 接口,在 `apps/api/backend/app.py` 的 `build_provider` 注册,
队列 / API / 前端无需改动。切换后端改 `config.json` 或环境变量
(`SWARMUI_PROVIDER` / `SWARMUI_COMFY_URL` / `SWARMUI_PORT`)。
隔离测试实例必须用 `SWARMUI_CONFIG` 指向独立配置文件(配合 `SWARMUI_DATA_DIR`),
否则会读取仓库根 `config.json` 并可能使用其中真实密钥产生计划外调用。

Python 后端使用 `.venv`，依赖见 `requirements-dev.txt`；`npm run verify` 同时运行前后端检查。单进程 JSON 存储，不开启多 worker。现有 API/SSE 和 data 格式变更必须有兼容测试。

## 抠图与前端构建

- `npm run build:web` 使用 `tsconfig.web.json` 编译画布 TS，并通过 `scripts/build-react.ts` 打包 React 工作台与落地页到 `.web-build/`；FastAPI 在原 `.js` 路径提供结果，不提交产物或手写同名 JS。
- 前端测试基于 `node --test` + tsx；DOM/组件测试经 `tests/dom.ts` 安装 jsdom 全局并配 Testing Library,纯逻辑测试保持无 DOM。
- `npm run dev` 自动编译并监听前端 TS；Docker 在 Node 阶段构建前端，运行阶段只有 Python。
- `apps/api/backend/cutout.py` 在后端进程的单推理线程中直接调用 rembg，不依赖 `CUTOUT_SERVICE_URL` 或 7100 端口；`CUTOUT_MODEL` 配置模型，首次请求加载。
- 识别操作使用 operationId、AbortController 和源对象身份检查；切换选择立即取消，迟到结果不得改写新会话。
- 取消请求不能强杀原生线程，线程实际完成前不得释放并发槽位。
- 不缓存 mask 或持久化笔画草稿；退出蒙版后再次抠图必须重新识别，禁止恢复旧选区。模型权重复用不等于图片识别结果缓存。已提交的抠图合成独立于识别会话。
