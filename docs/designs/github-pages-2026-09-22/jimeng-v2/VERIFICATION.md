# 第二版验收记录

后续定位修正：根据用户确认，官网与 README 以“外接云模型的创作平台”为主线。自部署仅指平台部署，生成通过云端 API；ComfyUI 只保留为可选技术说明。首屏与桌面/手机/README 预览已重新截图，手机无溢出，资源加载正常，git diff --check 通过。本轮仅调整文案及截图，未重复运行业务测试。

参考即梦官网实际页面，使用 draw-ui 的参考关系提取与完整页面验收流程。实现结果为 `website/index.html` + `website/cinematic.css`，第一版 CSS 存档于上层 `previous-style.css`。

## 视觉检查

已查看 1440×900 首屏、1440px 完整页面和 390×844 移动首屏。全屏原创影像、居中标题、青色 CTA、深色背景和交错的大图章节延续参考方向；保留 FRAYUNE 的真实安装与功能内容。

手机标题先前将“成为”断开，已用两个不可内部换行的短语修正为“让灵感，／成为下一帧。”。完整长页包含能力、工作流、安装、文档、FAQ 和页脚。

README 封面换成第二版首屏；`readme-preview.png` 使用 marked 在本地近似 GitHub 样式渲染，不是线上 GitHub 截图。第一版封面和整页截图保留。GitHub 自身排版、主题和图片大小由其页面决定，README 不运行首屏动画。

## 行为与构建

- 320、390、768、1024、1440px：文档无横向溢出，图像全部加载。
- 内部锚点存在，FAQ 可展开，本地静态资源引用无缺失。
- 背景开关勾选后 animation-play-state 为 paused，取消勾选为 running。
- prefers-reduced-motion 为 reduce 时 animation-name 为 none，隐藏无效动画开关。
- npm run verify 通过：typecheck、build、lint，前端 237 项测试通过；后端 300 项通过、2 项跳过。仍有一条既有 anyio 弃用警告。
- git diff --check 通过。

## 边界

首屏动效是静态 AI 图像的平移缩放，未生成真实视频；页面已标注。未下载即梦官方媒体用于产品，原创素材提示词见 PROMPT.md。参考截图仅用于本地设计记录，发布产物只含 website 目录。

尚未提交、推送或部署。GitHub Pages 工作流沿用第一版，应用内落地页、后端及画布未改动。
