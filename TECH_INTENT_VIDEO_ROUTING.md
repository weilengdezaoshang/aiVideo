# 技术方案：Host 意图识别与视频/多图路由（生产级 · v2）

> 相对 v1：补齐工具链契约、一步图生视频 vs 多帧决策、HITL/积分/写回、成本并发、迁移灰度、生产场景分层（P0–P2）。

## 1. 背景与问题

用户要视频时，Planner 曾输出「只生一张关键帧静图」并结束——属**路径发明错误**，不是单纯「没意图」。

根因：意图未结构化、计划无模板门禁、视频工具缺失时可静默降级为图。

## 2. 目标 / 非目标

**目标**
- 结构化意图 + 路径模板 + PlanValidator，禁止「goal=video 却单图结束」。
- 明确两条主路径及与「一步图生视频」的关系。
- 说清工具、积分、写回、clarify、观测与分期；列出生产场景哪些 P0 必做、哪些登记后续。

**用户主路由（产品已定）**
| 条件 | path |
|------|------|
| 有参考图 + 要主体/效果延续做视频 | `image_to_multiframe` |
| 无图 + 只要描述做视频 | `text_to_video` |
| 明确只要图 | `text_to_image` / `image_edit` |

**非目标**：本文件不实施自有桶后台、局部改图 UI 细节、真实 VLM 必选；这些另项，但输入校验预留钩子。

## 3. 架构

```
TurnInput(userText, canvasContext, flags, quota)
  → IntentGate(规则快筛 / 可短接)
  → IntentLLM(结构化 JSON，低温度)
  → IntentValidator
  → path=clarify? → HITL(挂起 turn，复用 interrupt 通道)
  → Planner(只填 path 模板参数)
  → PlanValidator(工具集合 + 终端产物类型)
  → CreditEstimator → interrupt_approval
  → TaskQueue 执行工具（可并行帧）
  → Reflection → NodeWrite(CAS 版本)
  → Trace(intent, planValidation, billing)
```

意图层不调用生成；Planner 不得自由发挥未登记路径。

## 4. 关键产品决策（v2 锁定）

### 4.1 有图时：多帧 vs 一步图生视频

配置 `IMAGE_VIDEO_STRATEGY`：
- `multiframe_first`（**默认，对齐你的描述**）：有图要视频 → 必须 `image_to_multiframe`。
- `allow_direct_i2v`：若用户明确「直接图生视频/一键成片」或 `frameCount=1` 且 feature 开，允许 path=`image_to_video`（单步）。
- **禁止**：无视频工具时用单次 `image_generate` 冒充。

### 4.2 写回规则

| 路径 | 中间产物 | 最终写回 |
|------|----------|----------|
| text_to_video / image_to_video | 无或临时 | **新视频节点**或当前空视频节点；不覆盖未确认的图片节点像素 |
| image_to_multiframe | 帧可写「子资产/隐藏节点」或仅存 storage refs | 最终 **video**；原参考图节点保留，除非用户勾选替换 |
| text_to_image | — | 图片节点 |

节点 CAS：`expectedAssetVersion` 不匹配则拒绝覆盖并提示冲突。

### 4.3 积分

- 确认卡一次展示**预估总价**（多帧：N×图价 + 视频合成价；文生视频：单次视频价）。
- 执行中某步失败：已成功步骤按策略退款或记部分消耗（配置 `BILLING_PARTIAL`）；**禁止**无确认加步。
- 预览两阶段（另案）预留 `stage: preview|final`，本方案 P0 不做。

## 5. 数据契约

### 5.1 输入

```ts
type CanvasContext = {
  projectId: string
  nodeId?: string
  nodeType?: "image" | "video" | "empty" | string
  assetUrls?: string[]
  assetVersions?: string[]
  hasImage: boolean
  selectedGroupIds?: string[]  // 选中组 → P0 若 >1 图则 clarify
  width?: number
  height?: number
}

type TurnInput = {
  userText: string
  canvas: CanvasContext
  flags: {
    videoToolEnabled: boolean
    multiframeEnabled: boolean
    directI2vEnabled: boolean
  }
  quota: { creditsLeft?: number; rateLimited?: boolean }
}
```

参考图 URL 进入意图前：上游域名白名单校验；失败则 clarify/错误，不规划。

### 5.2 IntentResult v1

```ts
type Goal = "video" | "image" | "region_edit" | "analyze" | "unknown"
type Path =
  | "text_to_video"
  | "image_to_multiframe"
  | "image_to_video"      // 仅 allow_direct_i2v
  | "text_to_image"
  | "image_edit"
  | "clarify"

type IntentResult = {
  version: "1"
  goal: Goal
  path: Path
  needSubjectCutout: boolean
  frameCount?: number         // default DEFAULT_FRAME_COUNT (4), max MAX (8)
  durationSec?: number
  referenceAssetIds: string[]
  confidence: number
  clarifyQuestion?: string
  clarifySlots?: string[]     // e.g. ["reference_image","duration"]
}
```

### 5.3 工具契约（P0/P1）

| Tool | 入参要点 | 出参 |
|------|----------|------|
| `subject_cutout` | sourceUrl | maskUrl / subjectUrl |
| `image_generate` | prompt, refs[], idempotencyKey | imageUrl |
| `video_generate` | prompt, duration, ratio | videoUrl |
| `image_to_video` | sourceUrl(s), prompt?, duration | videoUrl |
| `frames_to_video` | frameUrls[], fps/duration | videoUrl |

全部经 TaskQueue；支持 cancel、幂等、超时。P0 若某工具未实现：对应 path 的 `flags.*Enabled=false`，校验阶段失败，**不改 path 偷偷生图**。

## 6. 意图分层

### 6.1 规则快筛 + 可配置词表

`config/intent_lexicon.yaml`：video/image_only/ignore_ref 等多语言词。  
短接：
- 要视频 ∧ 无图 → `text_to_video`
- 要视频 ∧ 有图 ∧ 非「只要静帧」→ `image_to_multiframe`（默认策略下）
- 只要图 → 图像 path  

含糊语（「动起来」「好看一点」）→ 不短接，进 LLM 或 clarify。

### 6.2 IntentLLM

低温度、JSON schema；禁止输出「用单张静图代替视频」类建议。confidence&lt;0.6 → clarify。

### 6.3 IntentValidator（硬门禁）

- goal=video ⇒ path ∈ {text_to_video, image_to_multiframe, image_to_video}
- text_to_video ⇒ !缺视频工具
- image_to_multiframe ⇒ hasImage ∧ multiframeEnabled（或可降级为 image_to_video 仅当 `allow_direct_i2v` **且**用户可接受——须在确认卡写明）
- image_to_multiframe ∧ !hasImage ⇒ clarify
- 选中多张主参考冲突 ⇒ clarify
- quota.rateLimited / credits 明显不足 ⇒ 提前错误，不进贵计划

## 7. Planner 模板与 PlanValidator

**text_to_video**：clarify? → credit → `video_generate` → reflection → write video  

**image_to_multiframe**：cutout? → N× `image_generate`（可并行，受 `MAX_PARALLEL_FRAMES`）→ `frames_to_video` 或 `image_to_video` → reflection → write video  

**image_to_video**：credit → 单步 → write video  

**PlanValidator 拒绝**：goal=video 且无视频类终端工具；仅一次 `image_generate` 结束；写回类型与 goal 不符。拒绝后至多自动重计划 1 次，再失败给用户。

## 8. HITL（clarify）与 interrupt 统一

- clarify 与积分确认共用「awaiting_user」：`interrupt_kind: clarify | credit`。
- 快照含 IntentResult 草稿；用户回复走 `resume_interrupt`，合并槽位后从 Validator 重跑，不必整段丢弃（除非用户改目标）。
- 超时（如 24h）作废，提示重新发起。

## 9. 成本、并发、超时

- `DEFAULT_FRAME_COUNT=4`，`MAX_FRAME_COUNT=8`，确认卡展示 N。  
- 帧生成：`MAX_PARALLEL_FRAMES`（默认 2）；合成视频串行在帧齐后。  
- 单工具超时可配；整 turn 墙钟超时后可续跑（ContinuationDraft）。  
- 部分帧失败：策略 `abort_all`（默认）或 `continue_min_frames`（≥3 才合成）——P0 用 abort_all。

## 10. 边界场景矩阵（意图相关）

| 场景 | 期望 |
|------|------|
| 无图 + 生成视频 | text_to_video |
| 有图 + 按主体做视频 | image_to_multiframe + cutout |
| 有图 + 明确一键图生视频 | image_to_video（需开关） |
| 有图 + 不要参考纯文生 | text_to_video |
| 明确只要关键帧/海报 | goal=image |
| 视频工具未配置 | `video_unavailable`，禁止生图冒充 |
| 多参考冲突 / 选中组 | clarify |
| 局部改 + 要视频同时 | clarify 优先级 |
| 欠费/限流 | 前置错误 |
| 参考 URL 非白名单 | 不规划，提示换图或转存 |

## 11. 生产场景分层（避免方案假装全做）

**P0 必做**：意图 JSON + 门禁 + PlanValidator；计划 UI 可见；thinking/工具折叠；视频不可降级为单图；credit；cancel/迟到保护；白名单拉参考图；Trace 字段；基础评测。  

**P1**：真多帧工具链；并行帧；clarify 体验；部分退款策略；节点 CAS 写回；右侧正式入口。  

**P2 登记不阻塞 P0**：自有桶后台；多人协作锁；跨天任务；上传视频作参考；音频口播；导出分享；供应商 A/B；完整合规人审台；无障碍；客服追单审计 UI。  

P2 在架构上预留：`projectId` 权限检查钩子、`audit_log` 写 Trace/billing id、存储驱动接口。

## 12. 迁移与灰度

- `INTENT_MODE=off|rules_only|rules_plus_llm`（生产 `rules_plus_llm`）。  
- `PLAN_VALIDATOR=enforce`（生产必须 enforce）。  
- 废止旧「可自由首帧代替视频」系统提示；灰度按用户/项目百分比。  
- 观测：`intent_path` 计数、`plan_reject_total`、`video_downgrade_blocked_total`（应为 0 次成功降级）、路径成功率、P95 时延。

## 13. 评测

流程（mock 工具）：无图视频 / 有图多帧 / 只要图 / 视频未配置 / 单图冒充计划被拒 / clarify。  
弱效果：最终资产 `contentType` 为 video；多帧 path 的 image 调用次数 ∈ [min,max]。  

## 14. 与 UX 同迭代验收

本方案 P0 与「计划可见 + 折叠 + 禁止视频变图」同一发布列车验收，避免只改后端用户仍不可读。

## 15. 成功标准

- 要视频时 Trace/计划不再出现「仅一次生图结束」且成功写回。  
- 有图主体默认多帧路径；无图直视频；开关控制一步 I2V。  
- clarify/积分可恢复；限流与工具缺失显式失败。  
- P0 评测全绿；P2 场景有清单不假装已交付。

## 16. 一句话

规则定边界、模型填槽、模板出计划、校验锁终端产物；多帧与文生视频分道，视频永远不能用一张静图交差。
