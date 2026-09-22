# aiVideo 独立仓库与 monorepo 迁移

迁移日期：2026-09-11。

## 路径与范围

- 原目录：`/Users/weilengdezaoshang/Documents/项目/codeden/aiVideo`。
- 新目录：`/Users/weilengdezaoshang/Documents/项目/aiVideo`。
- 使用目录 rename 保留整个仓库；移动前后 Git HEAD 与 porcelain 工作区状态逐字节一致。
- 之后将 `backend/` 移到 `apps/api/backend/`，将 `web/` 移到 `apps/web/`。
- 根目录统一管理 npm workspaces，前端运行依赖归属 `@frayune/web`，Python 环境与配置继续放在根目录。
- 原目录不创建兼容符号链接。工具中保存的旧项目目录需要改为新目录。
- 本次只整理项目和路径，尚未实施完整评测平台。评测技术文档及 Agent 提示词已同步源码路径。

## 常用命令

从新仓库根目录执行：

```bash
npm run dev
npm run verify
npm run build --workspace @frayune/web
npm run dev --workspace @frayune/api
```

Python 模块位于 `apps/api`，启动桥接脚本和 pytest 已配置导入路径。直接运行模块可使用：

```bash
PYTHONPATH=apps/api .venv/bin/python -m backend
```

评测 CLI 仍是 `scripts/eval.py`。HTTP 页面路径和 API 不变；编译产物仍放 `.web-build/`。运行数据仍在根目录 `data/`。

## 环境处理

本机已有 `.venv` 的文本入口和激活脚本已重写原项目绝对路径；解释器及依赖实际验证通过。跨机器或 Python 安装位置变化时重新创建环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
npm ci
```

npm 使用现有锁定版本离线刷新 workspace 链接，没有升级业务依赖。Dockerfile 已同步 workspace manifest、源码 COPY 和 Python 导入路径。

## 验证

- `npm run verify`：类型检查、前端构建、ESLint、109 项前端测试、75 项后端测试通过。
- `npm ls --workspaces --depth=0`：正确识别 `@frayune/api` 与 `@frayune/web`。
- 后端测试包含现有 Mock 生成、评测 CLI、API 与静态资源兼容检查。
- Docker 镜像本次未构建；未调用真实生成模型或修改线上数据。

- 实际启动冒烟：使用临时数据与 Mock Provider，health、工作台、画布及编译 JS 均返回 200；验证后服务已停止。
