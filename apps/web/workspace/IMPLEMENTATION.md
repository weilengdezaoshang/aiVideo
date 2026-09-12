# React 创作首页

入口 `/workspace`；兼容 `/workspace/`、`?tab=overview`、`?tab=canvas`，新增 `?tab=assets`。品牌介绍页 `/` 和画布编辑器 `/canvas` 沿用现有实现。侧栏依据 V2 固定品牌、位置、顺序和视觉，窄屏收起文字。

## 组件与状态

先建立 `web/ui` 公共层，再由 `HomePage.tsx` 组合业务：

- `primitives.tsx`：Button、IconButton、SectionHeader、Tabs、Select、SearchInput、Dialog、EmptyState。
- `Sidebar.tsx`：共享侧栏，三个页面保持同一导航。
- `media-cards.tsx`：ProjectCard、InspirationCard，封面和交互状态由 props 提供。
- `Composer.tsx`：两种生成模式、参考图上传、动态模型与比例、视频时长和校验。
- `HomePage.tsx`：生成区、最近项目、灵感分类/搜索/详情和提示词复用。
- `AssetLibrary.tsx`、`SettingsDialog.tsx`：实际素材 API 与后端配置/连接测试。
- `api.ts`：显式 API/文档/素材类型，幂等创建与任务提交。保存节点早于请求入队，复用原有画布节点格式、SSE 和断线恢复，不修改已有 API/data 格式。

Select 包装现有 `web/shared/dropdown.ts`，不重新实现菜单定位、键盘控制与弹出层。公共样式在 `web/ui/primitives.css`；业务布局在 `workspace.css`，保留可见焦点、原生对话框焦点约束、减少动态偏好。新代码均使用 TS/TSX，无 any。

## 构建

React 仅接管工作台子页面，通过 esbuild 打包到 `.web-build/workspace/app.js`，继续由 FastAPI 提供；无需 CDN。原 `web/workspace/app.js` 已迁移删除。

`npm run build:web` 编译现有画布 TS 后执行 `npm run build:react`。后者运行 `scripts/build-react.ts`。`npm run dev` 同时监听原画布与新 React 模块。TSX 已纳入 typecheck、eslint、lint-staged 和 verify；Docker 前端阶段复制同一构建脚本。

## 素材

`assets/` 下四张独立图片使用内置 imagegen 生成：sailboat、flower、perfume、creature。没有把设计稿或界面文字作为页面背景。第二排复用 `web/landing/assets/` 已有素材；具体画面与生图设计稿有差别，名称和提示词与实际素材相对应。

灵感是示例图片，没有配套视频，因此不显示误导性的播放标记或时长。最近项目完全来自真实文档，不插入虚构用户项目；缺少封面时显示图标，无文档时显示新建入口。

生成提示词：
- 帆船：Cinematic photograph, landscape 4:3. Small sailboat with coral-orange sail on sparkling turquoise Mediterranean sea, hazy mountainous islands, natural sunlight, generous sea and sky.
- 花朵：Fine art macro photograph, portrait 4:5. Translucent apricot-orange poppy, glasslike delicate petals, fine veins, dark stamens, deep teal soft background.
- 香水：Luxury product photograph, square. Sculptural amber glass perfume bottle without branding on honey sandstone, sunlight, long shadow and glass refraction.
- 小兽：Cinematic original fantasy creature, portrait 4:5. Tiny white furry woodland creature in lush moss forest, natural sunbeams, emerald bokeh.
- 共用约束：Standalone production website gallery image, edge-to-edge, no UI/text/watermark/collage.

## 验收与边界

使用 `/tmp/frayune-react-preview/site` 的独立 Mock 服务及测试文档；没有修改用户 data。浏览器验证筛选、搜索空态、详情、提示词复用、比例、视频缺参考图禁用、上传、图片生成后跳转画布并完成结果回填、画布列表与重命名、设置连接测试，以及390px窄屏无横向溢出。验收图保存在 `output/draw-ui/frayune-creation-home-20260910/implementation/`。

自动化覆盖：当前后端能力校验、搜索交集、创建/保存/提交各阶段的响应丢失重试（同一文档和同一请求）、节点先保存再提交、TSX未构建503和工作台URL兼容。视频控件按实际Provider开放；未用真实ComfyUI/云端产生付费结果。重绘和抠图快捷入口说明并进入已有画布流程，不在首页重复实现蒙版工具。

## 画布与素材库延展

`CanvasLibrary.tsx` 和 `AssetLibrary.tsx` 实现两张库页面，公共页头、工具栏、卡片操作菜单、媒体预览与视图切换抽离到 `web/ui/library.tsx`，样式在 `library.css`。查询、排序、分组及上传校验集中在 `library-model.ts`。侧边栏保持原有组件和样式。

素材上传支持40MB内的JPG、PNG、WebP、MP4和WebM。上传接口可选`name`字段保留文件名，旧数据兼容使用短ID名称。素材页当前显示最近200项，搜索与排序作用于这些已加载数据。图片裁切仅用于缩略图，预览查看原文件；视频时长仅使用浏览器读取到的真实元数据。

完整验证记录：`output/draw-ui/frayune-library-pages-20260910/implementation/verification.md`。截图来自隔离Mock数据，真实视频播放尚未实测。
