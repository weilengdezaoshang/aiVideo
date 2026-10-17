# 技术方案：分层意图识别机制（v1.2）

> 原版日期：2026-10-15 · v1.1 修订：2026-10-15 · **v1.2 修订：2026-10-16**  
> 产品锁定：先排除不可能 → 大类 → 具体意图 → 细节参数 → 拿不准再问 → 计划执行  
> **v1.2 核心变更**：生产复杂场景下**不以正则包打天下**；L1/L2 主路径改为 **embedding 相似意图 + 少量强规则门闩**；正则仅作否定/强形式词/入口冲突等确定性信号。  
> 范围：主干 **aiVideo · 8443**  
> 与 `TECH_INTENT_ALIGNMENT.md` 冲突时 **以本文为准**

### 修订摘要（v1.2）

1. 明确：**纯正则无法覆盖生产复杂说法**（否定嵌套、风格义、指代、复合、口语）。  
2. L1/L2：**exemplar embedding 检索 + 分数档位**；正则降为门闩与槽位提示。  
3. 保留 v1.1：possibleL2/executable 拆分、发送一致性、clarify 边界、不降级生图、评测埋点。  
4. P0 即上「本地/侧车 embedding」规则库可跑；不依赖「先上线攒数据再上模型」才能处理复杂句。

**仍锁定（不得改）**：总流程；视频不可执行明确失败不降级；有图默认 `image_to_multiframe`；无 context → 400；clarify 绑定原快照；L3 不推翻 L2；P0/P1/P2 分期骨架；冲突以本文为准。

---

## 1. 一句话

代码先排除不可能 → **embedding 在可行集合里找最像的意图** → 强规则只做门闩与否定 → 弱/冲突就问 → 再填 L3、校验、执行。正则不再当主分类器。

---

## 2. 为什么正则不够（生产现实）

现网 `resolve_context` 与 `plan_intent` **两套关键词**，已出现：`animate`/`动起来` 一边认、一边不认。更严重的是复杂句：

| 类型 | 例子 | 正则痛点 |
|------|------|----------|
| 口语弱视频 | 「让画面活一点」「给点动态」 | 无固定词 |
| 风格义 | 「动画风格海报」 | 「动画」误触视频 |
| 否定嵌套 | 「别做成视频，就换个水彩风」 | 词序一变就漏 |
| 指代+选区 | 「按这个感觉再来一张」 | 无形式词 |
| 复合 | 「先修一下再出 5 秒」 | 单标签装不下 |

结论：生产必须以 **语义相似** 为主路径；词表/正则只处理「几乎不会错」的门闩。

---

## 3. 整体流程（v1.2）

```
用户点发送
  │
  ① 冻结快照（点击瞬间选区；ensureSaved；节点改动则拒发）——同 v1.1 §2①
  │
  ② 节点级校验（存在/权限/节点变化）——文档冲突归 ⑨ CAS
  │
  ③ 结构可行 → possibleL2；能力开关写入 executable 标记
  │    · @ 优先于选区；无有效图则剔除改图/有图做视频
  │    · 视频未开：视频 L2 仍留在 possibleL2（防被挤成改图）
  │
  ④′ 强规则门闩（正则/入口，确定性，可跳过 embedding）
  │    · 挂起 clarify / 样片确认 → 绑定原决策，结束识别
  │    · 强否定视频、强「只要图」、入口↔文字硬冲突 → 直接改写 possibleL2 或强制 clarify
  │
  ④⑤ Embedding 意图检索（主路径）
  │    · 查询向量 = embed(用户话术 [+ 轻量结构特征文本])
  │    · 只在 possibleL2（及 L1）对应的 exemplar 库里检索
  │    · 得到 topK：意图标签 + 相似分 → 映射 certainty: strong|weak|none
  │
  ⑥ L3：规则/轻量抽取风格、时长、ref_role；缺省默认（风格词不回写 L2）
  │
  ⑥′ executable：能力、积分（L3 后）、限流、内容安全；区分错误码
  │
  ⑦ 决策：strong 执行；weak 且含高代价 → clarify；冲突/none → 兜底或问
  │
  ⑧⑨ 计划校验 → 积分策略 → 队列 → CAS 写回
```

---

## 4. 每步谁来做

| 步骤 | 谁 |
|------|-----|
| ①②③⑥′⑦⑧⑨ | **代码** |
| ④′ 强规则门闩 | **少量正则/入口**（白名单级，可单测锁死） |
| ④⑤ L1/L2 | **embedding 检索**（主）+ 门闩修正 |
| ⑥ L3 | 规则抽取 + 默认；可选轻量模型补槽（P1） |

原则：模型/向量只在 **possibleL2 内**投票；不能发明 ③ 已排除的意图。

---

## 5. Embedding 意图机制

### 5.1 Exemplar 库（可运营）

每个意图标签维护一组**例句**（中英都要），带元数据：

```ts
interface IntentExemplar {
  id: string;
  l1: "generate_edit" | "canvas_organize" | "dialog_state" | "other";
  l2?: "text_to_image" | "image_edit" | "text_to_video" | "image_to_multiframe" | "image_to_video";
  text: string;                 // 例句
  tags?: string[];              // 如 style_sense / weak_motion
  rule_version: string;
}
```

建议分库检索：先 L1 库，再在命中 L1 下的 L2 库；或一次检索但 **过滤** `l2 ∉ possibleL2` 的命中。

覆盖至少：改图、文生图、文生视频、有图做视频、澄清样例、unsupported（编辑视频/对齐分组）、问候问用法、风格义「动画风」、否定视频、弱动态口语。

### 5.2 查询构造

```
query_text = user_message
  + "\n[选区:" + (hasImageRef ? "有图" : "无图") + "]"
  + (entryMode ? "\n[入口:" + entryMode + "]" : "")
  + (mentions ? "\n[@:" + n + "]" : "")
```

结构特征只进 query 侧提示，**不替代** ③ 的硬排除。

### 5.3 分数 → 档位（阈值用占位，内测定）

| 条件 | certainty |
|------|-----------|
| top1 ∈ possibleL2 且 score ≥ **[T_strong]** 且 (top1−top2) ≥ **[T_margin]** | `strong` |
| top1 ∈ possibleL2 但分数或边距不足；或 top1/top2 分属改图 vs 视频 | `weak` |
| 无命中 / 低于 **[T_min]** | `none` → 走兜底表 |

**禁止编造具体阈值**；上线前用评测集标定，写入配置 `intent_embedding.toml`（或等价），`rule_version` / `embed_model_id` 一并落 Trace。

### 5.4 与正则门闩的分工

| 信号 | 谁处理 |
|------|--------|
| 「做成视频」「生成视频」等**极强**形式 | 门闩可直接抬视频 L2（仍须 ∈ possibleL2） |
| 「不要视频」「只要图」等否定 | 门闩从候选中压制视频 / 抬 image |
| 「动画风格」「anime style」 | **exemplar 标注 style_sense**，embedding 学到；门闩可辅助把「动画」从强视频词剔除 |
| 「动起来」「活一点」「animate」 | **主要靠 embedding + 视频/改图边距**；弱则 clarify |
| 风格词「水彩」 | **不进 L2**；L3 抽取或 exemplar 附带 style 槽提示 |

v1.1 的「形式词三档」保留为**门闩辅助表**，不再作为唯一 L2 分类器。

### 5.5 基建选型（实现约束）

| 项 | 要求 |
|----|------|
| 模型 | 多语 embedding（中英）；固定 `embed_model_id`，升级要重嵌全库并升 `rule_version` |
| 检索 | 进程内向量（exemplar 量级通常百～千）即可；过大再上 ANN |
| 延迟 | P0 同步调用，预算 **[延迟占位，内测定]**；失败 → 降级：仅门闩+兜底+clarify，**不得瞎选视频** |
| 隐私 | 只 embed 当前句与结构标签；不把节点全文/像素送进向量服务 |

---

## 6. Feasibility（同 v1.1 精神）

- `possibleL2`：结构（选区/@/入口/节点类型）。  
- `executable`：能力、积分（L3 后）、限流、安全。  
- 判意图只看 possibleL2；视频未开不剔视频候选。  
- 不可执行：区分 capability_off / insufficient_credits / rate_limited / content_blocked。

---

## 7. 决策（⑦）与 clarify

| 情况 | 动作 |
|------|------|
| certainty=strong | 执行（过 executable） |
| certainty=weak 且 top 含视频 vs 改图 | **clarify**（只问 L2） |
| 门闩否定 vs embedding 视频 | 以否定门闩为准或强制 clarify（推荐：否定优先） |
| certainty=none | 兜底（无图→文生图；有图无视频意→改图；问候→other…） |
| unsupported exemplar 命中 | 明确文案，不进生成 |

clarify 边界（TTL、新消息作废、文字仅匹配选项、request_id 幂等、画布变化、最多一轮）**同 v1.1 §6.1**。

风格词与视频共现：若 embedding/门闩已定视频 → **不澄清**，L3 填 style；真·先改图再视频仍 **P2**。

---

## 8. 选区组合 / 有效 image / 发送一致性

沿用 v1.1：**§8 组合表、有效 image、§2① 发送一致性、§9 CAS/积分策略**。  
代码核对结论仍有效：先 capture 再 ensureSaved；节点改动拒发待补。

---

## 9. IntentDecision

```ts
interface IntentDecision {
  l1: "generate_edit" | "canvas_organize" | "dialog_state" | "other";
  l2?: "text_to_image" | "image_edit" | "text_to_video" | "image_to_multiframe" | "image_to_video";
  l3: Record<string, unknown>;
  path: string;
  certainty: "strong" | "weak" | "none";
  confidence?: number;          // 可用相似分归一；P1 再校准
  needsClarify: boolean;
  clarifyFocus?: "l2" | "l3" | "ref";
  signals: string[];            // 门闩命中 id + exemplar ids
  embed_model_id: string;
  embed_scores?: { label: string; score: number }[];
  rule_version: string;
  feasibilitySnapshot: Feasibility;
}
```

---

## 10. 分期（相对 v1.1 调整）

**P0（生产可上，复杂句可工作）**

- ①②③⑥′⑦⑧⑨ + 门闩  
- **Exemplar 库 + embedding 检索**（L1/L2）  
- clarify、发送一致性节点比对、无 context 收口  
- 评测集（含口语弱动态、风格义、否定、英文 animate）标定 **[T_strong]/[T_margin]/[T_min]**  
- embedding 失败降级策略  

**P1**

- 小模型 / 更大 embedding 或 cross-encoder 重排不确定分支  
- L3 槽位增强；阈值与 `confidence` 在线校准  
- 错例回流自动建议新 exemplar（人工确认后入库）  

**P2**

- 真复合「先改图再视频」多步意图  
- 画布整理等 L1 能力产品化  

---

## 11. 验收（增补 embedding）

| ID | 场景 | 期望 |
|----|------|------|
| E01 | 「让画面活一点」+ 选中图 | weak→clarify 或视频候选；不得静默文生图 |
| E02 | 「动画风格海报」+ 无视频意 | 图路径 + style；不得视频 |
| E03 | 「别做成视频，换水彩」+ 选中图 | image_edit |
| E04 | `animate this image` + 选中图 | 视频族 / clarify；不得直接改图 |
| E05 | embedding 服务超时 | 降级门闩+兜底/clarify；不得默认视频 |
| E06 | 视频能力关 + 明确要视频 | 识别为视频意图后 **明确失败**，不改图 |

原 M01–M10 / S01–S02 / L01 仍有效。

---

## 12. 评测与埋点（在 v1.1 §15 上增）

- 埋点增加：`embed_model_id`、topK scores、命中 exemplar_id、是否走门闩短路、是否降级。  
- 指标增加：embedding 参与率、降级率、改图↔视频在「无关键词句」上的准确率。  
- Exemplar / 阈值变更须跑评测；**高成本误判率不得变差**。  
- P0→P1 阈值仍用 **[X]**，内测定。

---

## 13. 默认决策（v1.2）

1. **L1/L2 主路径 = embedding**；正则只做门闩。  
2. 只在 `possibleL2` 内检索。  
3. 弱分且改图↔视频 → 必须问。  
4. embedding 失败 → 保守降级，不赌视频。  
5. 其余锁定项同 v1.1（不降级生图、multiframe 默认、clarify 绑定等）。

---

## 14. 待产品确认（继承 v1.1 §17，并增）

| # | 问题 | 备选 | 推荐默认 |
|---|------|------|----------|
| P1–P8 | 同 v1.1（多图、视频模式选区、TTL、积分、I2V、legacy、发送 A、澄清期节点） | — | 同 v1.1 推荐 |
| P9 | embedding 部署 | 本机模型 / 侧车 HTTP / 云 API | 本机或内网侧车，固定模型 id |
| P10 | 无关键词弱动态默认 | 一律 clarify / 偏改图 / 偏视频 | **一律 clarify**（生产复杂、视频贵） |

---

## 15. 待核对清单

| # | 事项 | 状态 |
|---|------|------|
| C1–C4 | 发送顺序、无换选区拒发、legacy | **已核对**（见 v1.1） |
| C5–C8 | 文档级预拒、积分 UI、旧端、@ | **待核对** |
| C9 | 仓库是否已有 embedding/向量依赖可复用 | **已核对（box 快照无意图 embedding 依赖；实现前以本机最新仓再 rg）** |
| C10 | Host 请求路径是否允许同步 embedding 延迟 | **待核对** |

---

## 16. 文档关系

- 本文 **v1.2**：分层 + **embedding 主路径**  
- v1.1：发送一致性 / clarify / 组合表等仍有效，冲突以 **较高版本** 为准  
- 实现提示词：`HANDOFF_LAYERED_INTENT_EMBEDDING.md`（已按 P9/P10 推荐默认写出）
