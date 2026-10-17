# 场景与效果：文字对齐图内元素 → 抠图

> 日期：2026-10-17  
> 配套方案：`TECH_TEXT_IMAGE_GROUNDING.md`  
> 产品对齐：低风险自动 apply；高风险才确认 / 去画布修正（`HANDOFF_CUTOUT_AGENT.md`）

本文给出 **6 个可画效果图的具体场景**：用户话术、图内状况、系统判定、期望 UX（auto / confirm / clarify）。实现与设计评审以本表为准。

---

## 场景总览

| ID | 话术 | 期望 UX | 一句话 |
|----|------|---------|--------|
| S1 | 抠出里面的人 | **auto** | 单人像，零点击出透明图 |
| S2 | 只要左边那个杯子 | **auto 或 confirm** | 双杯，空间槽位消歧；分差不够则确认 |
| S3 | 帮我抠图 | **auto** | 无指代，整图 rembg，不调 grounding |
| S4 | 抠人（图中三人） | **confirm / clarify** | 多主体，禁止盲自动 |
| S5 | 精细抠一下发丝 | **confirm → 画布** | 高风险修边，工具条兜底 |
| S6 | 先把人抠出来再做成视频 | **clarify** | 顺位复合，P0 不连跑 |

---

## S1 · 单人像「抠出里面的人」→ 自动

**画面**：一张半身人像海报，背景简洁，画布已选中。

**用户**：`抠出里面的人`

**槽位**：`op=matting`, `target_text=人`, `class_hint=person`, `needs_grounding=true`

**Grounding**：1 个高分 person 框 + mask，`score ≥ T_auto`

**风险**：低

**期望 UX（效果图要点）**

1. 右侧过程条：识别主体… → 正在抠图… → 完成（缩略图）  
2. **不出现** CutoutControls 工具条  
3. 画布源图右侧（或旁侧）出现透明 PNG  
4. 轻量「撤销本次抠图」

```
┌──────────── 画布 ────────────┐  ┌── 过程 ──┐
│  [人像原图]  →  [透明人像]   │  │ ✓ 抠图完成 │
│                              │  │ 已放到画布 │
│                    [撤销]    │  │ [缩略图]  │
└──────────────────────────────┘  └──────────┘
```

---

## S2 · 「只要左边那个杯子」→ 空间消歧

**画面**：桌面两个杯子，左杯白、右杯蓝，已选中整图。

**用户**：`只要左边那个杯子`

**槽位**：`target_text=杯子`, `spatial=left`, `class_hint=product`, `needs_grounding=true`

**Grounding**：2 候选；应用 spatial 后 top1=左杯；若 `top1−top2 ≥ T_margin` → 低风险自动；否则高风险确认。

**期望 UX**

- **分差足够（auto）**：过程条直接完成；结果仅为左杯透明图；可撤销。  
- **分差不足（confirm）**：原图叠加两个半透明框，**左框高亮**；按钮：「确认抠左杯」｜「选右杯」｜「去画布修正」。

```
确认态示意：
  原图上 [框1 左·高亮] [框2 右·淡]
  卡片：确认抠「左边的杯子」  [确认] [去修正]
```

---

## S3 · 「帮我抠图」→ 整图去背景（跳过 grounding）

**画面**：单件商品白底不足，需去背景。

**用户**：`帮我抠图` / `去掉背景`

**槽位**：`needs_grounding=false`（无指代/空间/索引）

**路径**：直接 rembg detect→ 质量过关则 **auto apply**

**期望 UX**：与 S1 同为零点击；过程文案用「识别主体 / 去背景」，不出现「匹配物体」步骤。  
**效果图差异**：无需候选框；蒙版≈整图主前景。

---

## S4 · 三人图 +「抠人」→ 必须拦

**画面**：合影三人并排。

**用户**：`抠人` / `把人抠出来`

**Grounding**：3 个接近分数的 person；或 `needs_grounding` 但无法唯一化。

**风险**：高（candidates > 1 且 margin 不足）

**期望 UX（禁止自动）**

1. 右侧预览：三人轮廓/三框  
2. 文案：「图里有多个人，请选一个」  
3. 点选其一 →「确认抠图」；或「去画布修正」  
4. P0 若无点选 UI：clarify「请说明左边/中间/右边，或点选」

```
┌ 预览卡 ─────────────────┐
│ 请选择要抠的人            │
│  [人A] [人B] [人C]       │
│  [确认抠图] [去画布修正]  │
└─────────────────────────┘
```

---

## S5 · 「精细抠发丝」→ 画布修正

**画面**：长发人像，发丝贴复杂背景。

**用户**：`精细抠一下发丝` / `我要改边缘`

**槽位**：`fine_edge=true` → **强制高风险**（即使 grounding 唯一）

**期望 UX**

1. 可先出检测预览，但 **不自动 apply**  
2. 主 CTA：「去画布修正」→ 打开 CutoutControls（画笔/橡皮/笔刷/对照原图/抠图）  
3. 可选次 CTA：「仍直接确认」（高级用户）

**效果图要点**：图下出现完整工具条；紫色蒙版；白底「抠图」按钮可见。

---

## S6 · 「先抠再视频」→ clarify（不连跑）

**画面**：任意单人像已选中。

**用户**：`先把人抠出来再做成视频`

**判定**：顺位复合；对齐 cutout P0 → **clarify**，不自动 grounding+apply+视频。

**期望 UX**

- 对话澄清：「目前可以先帮你抠图；做成视频需要你确认后再做。要先抠图吗？」  
- 用户确认「先抠」→ 再走 S1/S4 风险逻辑  
- **不出现** 静默视频任务；不出现 Provider 假抠图

```
Agent：这次包含两步（抠图 → 视频）。
      建议先完成抠图。是否先抠出主体？
      [先抠图] [取消]
```

---

## 效果图交付建议（设计 / 前端）

| 场景 | 建议帧 |
|------|--------|
| S1 | 过程条完成 + 画布双图（原/透明）+ 撤销 |
| S2 | auto 一帧 + confirm 一帧（双框高亮左） |
| S3 | 与 S1 类似，文案「去背景」 |
| S4 | 三框点选预览卡 |
| S5 | CutoutControls 展开态 |
| S6 | 澄清气泡双按钮 |

标注规范：每帧角标写清 `UX=auto|confirm|clarify`，避免评审时与旧「必点抠图」铁律混淆。

---

## 与风险闸门对照（速查）

| 场景 | candidate 数 | 其它信号 | UX |
|------|--------------|----------|-----|
| S1 | 1 高分 | person | auto |
| S2 | 2 | spatial=left | auto 若 margin 够，否则 confirm |
| S3 | — | 无 grounding | auto（rembg） |
| S4 | ≥3 或近分 | 多 person | confirm/clarify |
| S5 | 任意 | fine_edge | confirm→画布 |
| S6 | — | 顺位复合 | clarify |

---

**文档状态**：6 场景均可直接用于原型与验收用例（可映射 G01–G03、G07、G10）。

---

## 示意效果图路径

已生成线框示意（非最终视觉稿）：

| 场景 | 文件 |
|------|------|
| S1 | `mockups/text-image-grounding/S1_auto_person.png` |
| S2 | `mockups/text-image-grounding/S2_confirm_left_cup.png` |
| S3 | `mockups/text-image-grounding/S3_auto_full_bg.png` |
| S4 | `mockups/text-image-grounding/S4_confirm_multi_person.png` |
| S5 | `mockups/text-image-grounding/S5_canvas_fine_edge.png` |
| S6 | `mockups/text-image-grounding/S6_clarify_then_video.png` |
