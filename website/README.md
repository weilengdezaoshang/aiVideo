# GitHub Pages 展示页

`index.html` 与 `cinematic.css` 是独立静态站点，不依赖生成 API，也不加载应用设置。素材全部使用相对路径，适配 `/aiVideo/` 项目路径。现有应用内 `/landing` 不受影响。

本地预览（仓库根目录）：

```bash
python3 -m http.server 7802 --bind 127.0.0.1 --directory website
```

访问 `http://127.0.0.1:7802`。

## 发布

仓库 Settings → Pages 的 Source 选择 GitHub Actions。将本次修改推送到 `main` 后，`.github/workflows/pages.yml` 上传并发布 `website/`。也可手动运行 GitHub Pages 工作流。目标地址为 `https://weilengdezaoshang.github.io/aiVideo/`，发布成功前不要将本地预览视为线上版本。

仅 `website/` 作为发布产物，禁止改为上传仓库根目录。页面中的源码和文档链接指向 GitHub；开始创作按钮跳到本地安装区，不链接不存在的静态 `/workspace`。

## 素材与维护

`assets/logo.svg` 和 `assets/logo-white.svg` 复用现有 FRAYUNE 标识；建筑、花与海洋图复用 `apps/web/landing/assets/` 的视觉参考。`assets/dream-ocean.jpg` 是内置 image_gen 生成的原创幻想主视觉。画布与时间线小图为 CSS 概念示意，不是产品运行截图。页面不承诺多轨编辑，也不将静态图片称为生成视频。

第二版参考即梦官网的全屏影像、居中标题、深色章节和青色 CTA，使用 FRAYUNE 自身品牌和内容。首屏是静态图像缩放效果，提供「暂停背景动效」开关；系统开启减少动态时自动停用。

产品定位以外接云模型为主：云端 API 承担图像与视频生成，平台组织创作与素材管理。自部署命令用于运行平台，不表示需要部署生成模型；Mock 和 ComfyUI 仅是开发演示或可选适配。

设计稿与第一版预览保存在 `docs/designs/github-pages-2026-09-22/`；当前第二版的参考截图、完整页面、移动端预览与 README 封面位于其中 `jimeng-v2/`。更新封面时从页面首屏截图，保持品牌标题、说明和图像的可读性；不要将设计稿里的示例命令用于安装文档。
