# SwarmUI MVP

SwarmUI 风格的 AI 图像生成最小可用版本(Web UI)。核心流程端到端可用:

> 输入提示词 → 提交生成任务 → 实时进度 → 图片进入网格 → 灯箱查看大图/复用参数/删除 → 历史持久化

内置 **Mock 演示后端**(零依赖、开箱即用,无需 GPU),并可一键切换到 **ComfyUI** 真实出图。

## 快速开始

```bash
npm install
npm run dev        # 开发模式(文件变更自动重启)
# 或 npm start
```

打开 <http://127.0.0.1:7801>,输入提示词,点击「✨ 生成」即可。

## 切换到 ComfyUI 真实出图

1. 本地启动 ComfyUI(默认地址 `http://127.0.0.1:8188`),并放置好 checkpoint 模型。
2. 修改 `config.json`:

```json
{
  "provider": "comfyui",
  "comfyUrl": "http://127.0.0.1:8188",
  "port": 7801
}
```

3. 重启服务,模型下拉会自动读取 ComfyUI 的 checkpoint 列表。

也可以用环境变量覆盖:`SWARMUI_PROVIDER=comfyui SWARMUI_COMFY_URL=http://127.0.0.1:8188`。

## 功能清单

- 提示词 / 反向提示词、模型选择、宽高(含预设)、步数、CFG、种子(-1 随机)、批量数量
- 采样器 / 调度器选择(选项来自当前后端;ComfyUI 下自动读取其可用列表)
- **图生图(img2img)**:上传 / 拖入参考图(自动缩放至 1024px 内),配合「重绘幅度」(denoise < 1)使用;
  ComfyUI 后端自动上传参考图并切换 LoadImage + VAEEncode 工作流
- **参数预设**:把当前全部参数保存为命名预设(localStorage),一键载入 / 删除
- 任务队列(FIFO,Mock 并发 2 / ComfyUI 并发 1)、实时进度条(SSE 推送)、取消任务
- 图片网格 + 灯箱:完整生成参数(含采样器与重绘幅度)、复用全部参数、仅复用种子、下载、删除、键盘 ←/→/Esc 导航
- 历史持久化到磁盘(`data/images/` + `data/history.json`),重启不丢,上限 500 张自动裁剪
- 后端健康状态展示(顶栏状态灯)

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 服务与后端状态 |
| GET | `/api/models` | 模型列表(来自 provider) |
| GET | `/api/samplers` | 可用采样器与调度器列表 |
| POST | `/api/generate` | 提交生成任务,返回 `{ jobId }`;`initImage` 字段可选传参考图 data URL |
| GET | `/api/jobs` | 进行中的任务 |
| GET | `/api/jobs/:id` | 单个任务状态 |
| DELETE | `/api/jobs/:id` | 取消任务 |
| GET | `/api/history?limit=100` | 历史图片(新→旧) |
| DELETE | `/api/images/:id` | 删除图片与记录 |
| GET | `/api/events` | SSE 实时事件(snapshot / job / image) |
| GET | `/images/:file` | 生成结果静态访问 |

示例:

```bash
curl -s -X POST http://127.0.0.1:7801/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"一只宇航员猫, 霓虹灯","model":"mock-diffusion-xl","steps":20,"batchCount":2}'
```

## 目录结构

```
aiVideo/
├── config.json              # 运行配置(provider / comfyUrl / port)
├── src/
│   ├── server.ts            # Express 入口:REST API + SSE + 静态资源
│   ├── validate.ts          # 生成参数校验与规范化
│   ├── store.ts             # 图片文件 + 历史记录持久化
│   ├── types.ts
│   └── services/
│       ├── job-manager.ts   # 任务队列:调度/进度/取消/事件
│       └── providers/
│           ├── provider.ts          # 生成后端抽象接口
│           ├── mock-provider.ts     # 内置演示后端(占位图)
│           └── comfyui-provider.ts  # ComfyUI HTTP API 对接
├── web/                     # 前端(原生 HTML/CSS/JS,无构建步骤)
├── tests/                   # node:test 单元测试
└── data/                    # 运行时产物(git 已忽略)
```

事件流:前端通过 SSE(`/api/events`)接收 `job`(进度)与 `image`(落盘)事件;后端 `JobManager` 把任务拆成单张图片依次调用 `GenerationProvider`,provider 只负责产出图片字节,落盘与记录统一由 `Store` 完成。

## 测试与检查

```bash
npm test           # 22 个单元测试(Store / MockProvider / JobManager / 参数校验 / ComfyUI 工作流)
npm run typecheck  # TypeScript 严格模式检查
```

## 工程化

- **一站式校验**:`npm run verify`(typecheck + eslint + 测试),提交前必跑;`npm run format` 统一格式。
- **与 codeden 的关系**:aiVideo 是独立 git 仓库(仅物理上位于 codeden 目录内),codeden 已忽略
  本目录、其 lint 也不扫描;本仓库自带完整工具链,可随时整体移出而不受影响。
- **Git 钩子**:husky + lint-staged(`npm install` 自动启用)—— pre-commit 对暂存文件执行
  Prettier + ESLint 并运行 typecheck;commit-msg 校验提交规范 `type(模块): 中文描述.`(见 AGENTS.md)。
- **CI**:GitHub Actions([`.github/workflows/ci.yml`](.github/workflows/ci.yml)),push / PR 触发
  `npm ci && npm run verify`,推送到 GitHub 后自动生效。
- **日志**:结构化输出(ISO 时间戳 + 级别),`LOG_LEVEL=debug npm run dev` 可看请求级访问日志。
- **健壮性**:启动时配置快检(非法 provider/端口直接报错)、404/500 统一 JSON 错误结构、
  非法 JSON 请求体返回 400、生成队列上限 50(超限 429)、SIGTERM/SIGINT 优雅停机。
- **Docker**:`docker compose up -d app` 起服务(挂载源码免构建);
  `docker compose --profile comfy up -d` 额外拉起 ComfyUI(8188),
  再以 `SWARMUI_PROVIDER=comfyui SWARMUI_COMFY_URL=http://comfyui:8188` 启动 app 即真实出图。
- **新增生成后端**:实现 `src/services/providers/provider.ts` 的 `GenerationProvider` 接口,
  在 `src/server.ts` 的 `loadConfig` 后注册即可,JobManager / API / 前端无需改动。

## 已知边界与后续路线

- ComfyUI 工作流为标准 txt2img / img2img(euler 系采样器可用参数选择),LoRA、ControlNet 等高级节点暂未暴露
- Mock 后端输出 SVG 占位图(同种子可复现;图生图模式下参考图按 1 - denoise 垫底),仅用于打通流程与 UI 演示
- 后续可扩展:LoRA、ControlNet、参数预设云端同步、多用户会话、
  视频生成后端(目录名 aiVideo 的预定期望,provider 抽象已为其留好位置)
