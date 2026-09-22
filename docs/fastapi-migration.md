# FastAPI 主后端迁移

本次迁移将 Express 主后端替换为 Python 3.11+ / FastAPI，保留现有画布前端与 API 协议，不包含新的图片/视频节点面板。

## 启动与验证

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
npm install
npm run dev
```

依赖由 `requirements.lock.txt` 约束到本次验证版本，包含运行时与测试依赖；生产安装 requirements.txt 只安装运行时依赖。

默认地址 http://127.0.0.1:7801。`npm start` 关闭热重载。百炼启动命令 `npm run bailian`、`npm run start:bailian` 均已切换为 FastAPI 主服务。Node 保留用于前端 TypeScript/ESLint/node:test 与辅助脚本，不运行主后端。

`npm run verify` 包含前端类型检查、ESLint、62 项前端测试，以及 Ruff 和 34 项 Python 测试。Python 测试中包含一个真实 TCP 测试：启动隔离 FastAPI 实例，运行现有 Node 冒烟脚本的 14 项检查，再验证 SSE 的 snapshot、job、image 和完成事件。测试需要允许监听本地临时端口。

## 保留的接口与数据

- `/api/health`、`models`、`samplers`、`config` 和候选配置测试。
- `/api/generate`、四方向生成、任务查询和取消、SSE。
- 历史检索、收藏、删除、生成文件下载。
- 素材二进制上传、缩略图、素材元信息。
- 画布 CRUD、幂等创建、revision 冲突检测、sendBeacon POST 保存。
- 抠图 detect/apply 与 Agent 会话的人工蒙版确认流程。
- 生成追踪 JSONL、删除反馈与指标聚合。
- 首页、工作台、画布、传统面板及静态资源。

保留 `config.json` 和 `SWARMUI_*` 命名；API Key 仅输出掩码。环境变量继续优先于文件配置。`SWARMUI_DATA_DIR` 可覆盖默认 data 目录；`SWARMUI_HOST` 可覆盖默认回环监听地址，Docker 使用 0.0.0.0。

原 `data/history.json`、`data/jobs.json`、`data/agent-sessions.json`、`data/documents/*.json`、`data/assets/<id>/meta.json` 与原始图片文件均原格式读取。此次开发/验证未清空用户 data，也未调用真实付费生成 API。

## Provider 与调度

- Mock：保留原模型与采样器列表；图片演示改用 PNG，视频演示为标记 MOCK 的动画 WebP。
- 云端：迁移智谱、硅基流动、OpenAI 兼容和阿里云百炼协议，保留原有能力边界。百炼默认模型带参考图时切到 qwen-image-edit-plus；硅基流动保留 Kontext/Fill；云端视频仍未接入。
- ComfyUI：图片、图生图、蒙版，以及 LTXV / Wan 单与双 UNet 工作流。`tests/backend/fixtures/comfy-workflows.json` 从迁移前代码生成，Python 构图与这些基准逐项相等。
- 云端 HTTP 使用本地模拟响应验证，ComfyUI 上传/提交/轮询/下载使用模拟上游验证。真实 GPU/厂商效果不属于本次验证结论。

队列保留 FIFO 和按 Provider 限制并发。批次开始时冻结 Provider 实例，热更新不会改变运行中的批次。任务状态和结果持久化；重启时中断中的任务标记失败，不自动重复付费请求。SSE 使用有界订阅队列，慢客户端断开后通过快照恢复。

运行采用单进程、单 worker。数据目录通过 POSIX 文件锁拒绝第二个服务进程（macOS/Linux）。多 worker/多机部署需要独立队列和事务数据库，不能简单增加 Uvicorn workers。大型图像解码、Mock 绘制与抠图合成在线程中处理；JSON 变更由服务进程串行提交。

## 清理范围

已删除原 `src/` TypeScript 后端，以及依赖它的 15 个 TypeScript 后端测试文件；新的 Python 契约测试覆盖替代接口和关键生命周期。`express`、`sharp`、`@types/express` 与对应传递依赖已从 npm 清单移除。rembg 后续已集成到 `backend/cutout.py`，不再启动独立识别服务，详见 `cutout-flow.md`。

原后端含本次迁移前未提交的改动，删除前已将原 src 与后端测试保存到本机 `/tmp/aivideo-before-fastapi-backend.tar.gz`；该临时备份不是部署依赖，不包含运行时密钥或 data。

已更新 npm 启动命令、Python 依赖清单、CI、Dockerfile、Compose、README 和 AGENTS 中的后端架构说明。既有版本设计文档保留为历史方案，不作为当前运行入口。

## 后续节点面板

图片/视频节点草稿、assetId 生成引用、幂等生成提交和主体库是下一阶段功能。本次只迁移现有行为，`/api/generate` 仍接受既有 data URL 参考图和 clientRef，避免将接口重设计与语言迁移混在一起。

## 旧版入口下线

迁移后进一步移除 `/legacy` 路由、工作台旧版入口及 `web/index.html`、`web/app.js`、`web/style.css`。旧地址返回 404，创作统一从 `/workspace` 和 `/canvas` 进入；生成、历史、素材 API 与用户 data 保留。
