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

- [快速开始](#快速开始) · [本地人像抠图](#本地人像抠图) · [Docker](#docker)
- [云模型接入](docs/百炼生图接入.md) · [可选 ComfyUI 适配](#切换-comfyui-真实出图)
- [API 一览](#api-一览) · [项目结构](#项目结构) · [测试与校验](#测试与校验)
- [FastAPI 后端迁移](#fastapi-后端迁移) · [时间线与 MP4 导出](#时间线与-mp4-导出)
- [品牌资源](docs/brand/frayune/README.md) · [项目主页维护](website/README.md)

</details>

## 代码与部署状态

新版平台源码已同步到本仓库：`apps/api` 为 Python/FastAPI 服务，`apps/web` 为 React/TypeScript 工作台与画布。云模型通过服务端 Provider 接入，API Key 不写入官网或浏览器静态资源。

[GitHub Pages 官网](https://weilengdezaoshang.github.io/aiVideo/) 仅提供产品介绍。运行创作平台请按下方步骤部署应用并配置云模型；发布源码不代表云 API 服务已经在线部署。

### 创作入口

百炼 `qwen-image-2.0` 文生图可运行 `npm run bailian` 一键启动，详见[完整配置步骤](docs/百炼生图接入.md)。后续多项目共用 Key 的规划：[统一模型网关方案](docs/统一模型网关方案.md)。

当前创作入口为 `/workspace` 与 `/canvas`，旧版独立生成面板已下线。

工作台 `/workspace` 与画布 `/canvas` 使用 React + TypeScript，画布渲染基于 Konva，公共组件位于 `apps/web/ui`。`npm run dev` 自动构建并监听前端；生产构建运行 `npm run build:web`。组件拆分、素材与验证说明见 [React 创作首页](apps/web/workspace/IMPLEMENTATION.md)。

<a id="功能概览"></a>

## 功能概览

- **云模型创作** —— 文生图、图生图、局部重绘与图生视频；实际可用模式由接入的服务和模型决定，不同模型的能力不完全相同。
- **生成参数** —— 选择模型、尺寸与批量数量；其余参数按所选服务能力提供，不要求云模型支持本地采样器、步数或 CFG。
- **任务队列** —— FIFO 调度,按后端并发能力执行;实时进度条(SSE 推送)、随时取消、失败后一键重试
- **结果管理** —— 画布预览、素材管理、下载与删除，生成历史继续保存在本机。
- **任务历史** —— 最近任务状态一览(状态点 + 重试入口)
- **持久化** —— 图片与历史落盘(`data/`),重启不丢;上限 500 条自动裁剪
- **健壮性** —— 参数校验、统一 JSON 错误、队列上限保护、优雅停机、结构化日志
- **云模型接入** —— 通过 Provider 统一适配外部模型 API；保留 Mock 开发演示和可选 ComfyUI 适配。

<a id="快速开始"></a>

## 快速开始

```bash
git clone https://github.com/weilengdezaoshang/aiVideo.git
cd aiVideo
npm install
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
npm run dev
```

以上命令启动创作平台，不会自动完成云模型配置。按[云模型接入说明](docs/百炼生图接入.md)配置服务地址、模型和 API Key 后再生成；百炼也可用 `npm run bailian` 配置并启动，访问地址以启动输出为准。未配置时的 Mock 仅供开发演示。

### 本地人像抠图

运行 `npm run dev`（百炼配置可用 `npm run bailian`）启动 FastAPI，后端内部直接调用 rembg，无需独立识别服务。
默认模型为 `birefnet-portrait`，首次使用会下载权重，之后离线推理，无需抠图 API Key。
点击图片的「智能抠图」后自动生成红色主体蒙版，可擦除/恢复；确认后在原图右侧生成透明 PNG。
人像模型以人物为目标，装饰和手持物品是否保留仍需检查实际选区。

需要恢复通用抠图时，运行 `CUTOUT_MODEL=isnet-general-use npm run bailian`。
`CUTOUT_MODEL` 传给 FastAPI 启动命令，修改后需重启。模型首次请求加载后复用；不缓存图片识别结果。`U2NET_HOME` 可指定模型目录。

### Docker

```bash
docker compose up -d app                        # 仅应用
docker compose --profile comfy up -d            # 应用 + ComfyUI
```

### 切换 ComfyUI 真实出图

以下为仓库保留的可选适配方案，不是云模型接入的必要步骤。

1. 本地启动 [ComfyUI](https://github.com/comfyanonymous/ComfyUI)(默认 `http://127.0.0.1:8188`)并放置 checkpoint 模型;
2. 修改 `config.json`:

```json
{ "provider": "comfyui", "comfyUrl": "http://127.0.0.1:8188", "port": 7801 }
```

3. 重启服务 —— 模型与采样器列表自动从 ComfyUI 读取,生成真实图片。

也可用环境变量覆盖:`SWARMUI_PROVIDER` / `SWARMUI_COMFY_URL` / `SWARMUI_PORT`。

### 图生视频(ComfyUI 真实出片)

视频工作流按模型自动选择:LTX-Video(单 checkpoint,放 `models/checkpoints`)走 LTXV 链路;
Wan 系模型(放 `models/unet`)走 UNETLoader 链路,同时配置 `wanHighNoiseUnet` + `wanLowNoiseUnet`
后按 Wan2.2 双模型分段采样。产物为 mp4(SaveVideo),画布内预览播放。

```json
{
  "videoModel": "",
  "videoBackend": "auto",
  "wanClip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
  "wanVae": "wan_2.1_vae.safetensors",
  "wanHighNoiseUnet": "",
  "wanLowNoiseUnet": "",
  "imageTimeoutMin": 20,
  "videoTimeoutMin": 60
}
```

`videoModel` 留空则自动探测(wan 优先于 ltx);`videoBackend` 可强制指定 `ltxv` / `wan`。
以上键均有对应环境变量:`SWARMUI_VIDEO_MODEL` / `SWARMUI_VIDEO_BACKEND` / `SWARMUI_WAN_CLIP` /
`SWARMUI_WAN_VAE` / `SWARMUI_WAN_HIGH_NOISE_UNET` / `SWARMUI_WAN_LOW_NOISE_UNET` /
`SWARMUI_IMAGE_TIMEOUT_MIN` / `SWARMUI_VIDEO_TIMEOUT_MIN`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 服务与后端状态 |
| GET | `/api/models` | 模型列表(来自 provider) |
| GET | `/api/samplers` | 可用采样器与调度器 |
| POST | `/api/generate` | 提交生成任务(202 + jobId);`initImage` 可选 data URL,`maskImage`(PNG,白 = 重绘)为局部重绘蒙版 |
| GET | `/api/jobs` | 进行中任务;`?all=1` 返回最近任务历史 |
| GET | `/api/jobs/:id` | 单个任务状态 |
| DELETE | `/api/jobs/:id` | 取消任务 |
| GET | `/api/history?limit=100` | 历史记录(新 → 旧) |
| PUT | `/api/images/:id/star` | 设置 / 取消收藏 |
| DELETE | `/api/images/:id` | 删除图片与记录 |
| GET | `/api/events` | SSE 实时事件(snapshot / job / image) |

```bash
curl -X POST http://127.0.0.1:7801/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"一只宇航员猫, 霓虹灯","model":"mock-diffusion-xl","steps":20,"batchCount":2}'
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
│       ├── canvas/              # 画布核心、状态与 React/Konva 渲染
│       ├── canvas-next/         # React 画布应用入口
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
├── config.json                  # 本地运行配置
└── docker-compose.yml           # FastAPI 镜像 + 可选 ComfyUI
```

**核心数据流**:

```
浏览器 ──POST /api/generate──▶ JobManager ──FIFO 队列──▶ GenerationProvider
   ▲                                │                      │  外部云模型 API
   └── SSE 实时渲染 ◀─ job/image 事件 ┘        产物字节 ──▶ Store 落盘 + 历史
```

新增生成后端实现 `apps/api/backend/providers/base.py` 的 `Provider` 抽象，并在 `apps/api/backend/app.py` 的 `build_provider` 注册。`generate` 通过 `params.kind` 区分图片与视频。

## 测试与校验

```bash
npm run verify          # 前端 typecheck / eslint / node:test + 后端 ruff / pytest
node scripts/e2e-smoke.mjs   # 对运行中的服务执行 14 项端到端检查
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


## FastAPI 后端迁移

主后端已经迁移到 Python，Express 与 sharp 服务端依赖已移除。现有 API 路径、SSE 事件名、`config.json` / `SWARMUI_*` 与 `data/` 格式保持兼容。Python 运行时为 3.11+；Node 仅用于前端校验与辅助脚本。

- `npm run dev`：FastAPI/Uvicorn 热重载，默认 `127.0.0.1:7801`。
- `npm start`：编译前端后启动，不启用热重载。
- `npm run build:web`：将 TypeScript 编译到 `.web-build/`，FastAPI 按原 `.js` URL 提供模块；`npm run dev` 自动编译并监听前端修改。
- `npm run bailian`：交互式配置百炼并启动 FastAPI；`npm run start:bailian` 使用已有 `.env.bailian.local`。
- `npm run verify:backend`：Ruff 与 Python 测试。
- `/docs`、`/openapi.json`：FastAPI 接口目录（生成请求的规范化由 models.py 完成）。

此版本采用单进程队列与 JSON 原子落盘，不要配置多个 Uvicorn worker；数据目录锁会拒绝第二个进程。`SWARMUI_DATA_DIR` 可指定隔离数据目录。重启时原进行中任务标记为中断，不会自动重复发起付费请求。切换后端时，运行中的批次使用原 Provider，后续调度使用新配置。

rembg 已集成到 FastAPI 的单推理线程，不阻塞 API 事件循环；忙时返回 429。取消立即使结果失效，原生推理线程自然结束后才释放槽位。Mock 图片为 PNG，Mock 视频为带 MOCK 标记的动画 WebP。

完整数据流、模块边界和异常处理见 [抠图流程](docs/cutout-flow.md)。

详细验证与兼容范围见 [FastAPI 迁移说明](docs/fastapi-migration.md)。

### 开发环境设置入口

生成设置仅在 `SWARMUI_ENV=development` 时开放。`npm run dev`（`--reload`）默认启用该模式；显式设置 `SWARMUI_ENV=production` 时不会被覆盖。普通启动和部署默认关闭设置。工作台与画布根据 `/api/runtime` 显示入口；生产环境拒绝配置读取、保存和连接测试接口（403）。生产密钥由部署环境变量注入，不通过用户设置录入。开发模式不是用户鉴权机制，不应对公网开放。


### 时间线与 MP4 导出

画布顶栏的「时间线」可将选中的图片或视频加入单轨剪辑，支持顺序调整、数值裁剪、分割、复制、删除和撤销。编辑结果随画布保存。

「导出 MP4」按提交时的时间线快照渲染 H.264/AAC 成片，保留视频原声，图片和无声视频补静音；可查看进度、取消并下载结果，刷新后恢复最近一次导出。支持本地资产库与历史生成素材，外部链接须先导入。单次最多 200 个片段、总长 1 小时，宽高为偶数且不超过 4096；多轨音频、字幕及转场尚未实现。

运行 `pip install -r requirements.txt` 安装固定版本的 `imageio-ffmpeg`，或提供系统 FFmpeg。导出任务及成片保存在 `data/exports/`，与生成任务分开管理。服务重启会保留完成记录，将中断任务标记失败，不自动重做。

接口：`POST /api/exports` 提交 `{documentId, requestId, timeline}`；`GET /api/exports/{id}` 查询，`DELETE /api/exports/{id}` 取消，`GET /api/exports/{id}/download` 下载成片。
