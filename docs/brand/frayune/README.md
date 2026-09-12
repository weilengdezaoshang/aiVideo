# 帧屿集 FRAYUNE

2026-09-10 新品牌设计候选，替换已发现同名冲突的帧序 FRAMORA。

**权利状态：公开命名初筛暂未发现完全同名产品；商标和图形权利清查未完成，不承诺无侵权、全球无重名、可注册或独占。**

## 品牌设计

- 中文：帧屿集（zhēn yǔ jí）。以“帧”连接图像与视频，“屿集”表达独立作品汇聚的创作空间。
- 英文：FRAYUNE，为本轮拟定的品牌词，建议读作“fray-yoon”。
- 定位：AI 视觉创作平台。
- 图形：三片不等宽的影页；各自不同的直边、曲率和收尾形成逐步展开的节奏。
- 配色：黑白。页面符号和中英文字标共同继承 currentColor，适配深浅主题。
- 英文字标：本项目逐字绘制 F/R/A/Y/U/N/E 几何路径，未采用下载的商业字标或图标素材。
- 中文字标：Noto Sans CJK SC Medium 的三个字形，依开放字体许可证生成路径，来源与哈希见下文。

## 文件

- [设计展示 PNG](brand-identity.png) / [展示 SVG](brand-identity.svg)
- [默认标志](../../../web/brand/logo.svg) / [反白标志](../../../web/brand/logo-white.svg)
- [横版组合](../../../web/brand/logo-lockup.svg) / [反白横版](../../../web/brand/logo-lockup-white.svg)
- [英文字标](../../../web/brand/wordmark.svg) / [中文字标](../../../web/brand/name-zh.svg)
- [应用图标](../../../web/brand/favicon.svg)
- [公开初筛记录](clearance-review.md)

正式 SVG 均使用路径，不嵌入位图，也不依赖用户设备上的字体。展示板的辅助英文说明采用系统字体；交付 Logo 本身不依赖该字体。

## 制作来源与复现

图形和英文字标的完整路径保存于 build-brand.py。运行 `python3 docs/brand/frayune/build-brand.py` 可重建当前 SVG 文件。这是几何向量设计，没有调用图像生成服务。

中文字形由 outline-chinese.swift 使用 CoreText 从 NotoSansCJKsc-Medium.otf 提取，结果保存于 chinese-paths.svgfrag。字体来自 [Noto 官方仓库](https://github.com/notofonts/noto-cjk/blob/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Medium.otf)，许可证为 [SIL OFL 1.1](OFL.txt)。下载日期2026-09-10，下载的原字体SHA-256：`ca094f6b0001fb048ca39ddd797a0cdb0179e1e55c6561e111c49c3e6a61d7b7`。仓库仅保存输出路径和许可证，原字体未作为应用资源分发。

开放字体许可和自行绘制的过程只能说明素材来源，不能替代对第三方商标及其他在先权利的核查。
