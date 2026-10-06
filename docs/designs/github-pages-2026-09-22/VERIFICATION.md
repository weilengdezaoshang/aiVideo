# 验证与交付记录

- 实现：`website/index.html`、`website/style.css`，纯 HTML/CSS，无应用 API 请求。
- 设计稿：内置 image_gen 生成 `concept.png`，完整长页方向探索。未调用第三方生图服务。
- 视觉判断：沿用设计稿的浅色编辑排版、左对齐标题、宽幅影像和深色流程区；使用现有真实品牌标识与参考素材替换生成稿中的虚构图片。实现没有设计稿的多轨编辑声明、不正确安装命令、社交账号和虚构网址。
- 布局检查：1440、1024、768、390、320px 宽度下文档宽度均不超过视口。390px 首屏文字、操作与图片可读。
- 资源与行为：页面图片全部加载；内部锚点均有目标；FAQ 点击可展开；资源相对路径和指向仓库文件的文档链接均检查。
- 修正：流程第三步图像补充容器高度约束，避免使用图片固有高度导致裁切到空白部分。
- `npm run verify`：类型检查、前端构建、ESLint 通过；前端 237 项通过；后端 300 项通过、2 项跳过。现有 anyio 弃用警告 1 条。
- `git diff --check`：本次修改无空白错误。

## 预览

- `preview/desktop-full.png`：1440px 完整页面，包含页脚。
- `preview/mobile-full.png`：390px 完整页面。
- `preview/mobile-viewport.png`：390×844 首屏，便于检查实际字号。
- `preview/readme-cover.png`：README 使用的品牌封面。
- `preview/readme-preview.png`：使用 marked/GFM 在本地渲染的 README 首段；为 GitHub 风格近似，不是 GitHub 线上截图，实际深浅色主题和排版由 GitHub 控制。

## 发布状态

仅准备 `.github/workflows/pages.yml`，尚未提交、推送或部署。工作流需要仓库 Pages Source 为 GitHub Actions，发布目录严格限定 `website/`。README 项目主页链接指向预定发布地址，线上可用性未验证。

已有 README 时间线说明和其他工作区改动保留。没有改动应用内落地页、画布、后端或本地密钥配置。
