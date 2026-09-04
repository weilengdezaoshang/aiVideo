# 开发约定

本仓库(aiVideo)是独立 git 仓库:SwarmUI 风格的 AI 生成应用。TypeScript + Express 后端,
原生 HTML/CSS/JS 前端,Provider 抽象支持 Mock 与 ComfyUI 后端。

## 常用命令

- `npm run dev` — 启动开发服务(默认 http://127.0.0.1:7801,tsx 热重启)
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
src/server.ts                    # Express 入口:REST + SSE + 静态资源 + 错误处理
src/services/job-manager.ts      # FIFO 任务队列:调度、进度、取消、事件
src/services/providers/          # 生成后端抽象:provider.ts 接口 + mock + comfyui 实现
src/store.ts                     # 图片与历史的磁盘持久化
web/                             # 前端(无构建)
```

新增生成后端:实现 `GenerationProvider` 接口,在 `server.ts` 的 `loadConfig` 后注册,
队列 / API / 前端无需改动。切换后端改 `config.json` 或环境变量
(`SWARMUI_PROVIDER` / `SWARMUI_COMFY_URL` / `SWARMUI_PORT`)。
