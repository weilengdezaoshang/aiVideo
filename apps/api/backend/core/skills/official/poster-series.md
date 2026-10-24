---
name: 海报系列
version: "1"
scenes:
  - 海报系列
  - 活动海报
  - 多风格方向草稿
  - 系列化出图
role: 海报设计：先出方向草稿，选定后统一风格系列化。
allowlist:
  - text_to_image_preview
  - text_to_image_final
  - image_analyze
  - create_asset_card
  - update_asset_card
phases:
  - name: 方向草稿
    tools: [text_to_image_preview]
    required: true
    confirm: true
  - name: 系列化正片
    tools: [text_to_image_final]
    required: true
routing:
  ask_first:
    - 主题或用途不明
    - 目标张数未定
  act:
    - 用户说了主题、场景和风格倾向
boundaries:
  - 不跳过方向草稿直接出正片
  - 画面内文字排版未开放，标题文案只写进提示词
  - 不编辑已有产品图（改用电商主图技能）
defaults:
  count: 1
---

# 海报系列手册

## 阶段 1：方向草稿（需确认）
1. 用 `text_to_image_preview` 出 2–3 个风格方向（每方向一张单子，提示词各自完整）。
2. 确认点：`ask_user` 列出方向让用户选定；未选定不得进入正片阶段。
3. 用户提到已有参考图时，先 `image_analyze` 看懂风格，或 `create_asset_card` 存风格卡再引用。

## 阶段 2：系列化正片
- 按选定方向逐张 `text_to_image_final`；系列内风格关键词保持一致（同一张风格卡或同一段风格描述）。
- 张数以用户或硬性要求为准；每张一个单子，不要用 count 合并不同构图。

## 写单要点
- 方向间的差异写在提示词主体（构图/元素），风格词保持一致。
- 某个方向失败不换风格冒充，报错并保留其余方向。
