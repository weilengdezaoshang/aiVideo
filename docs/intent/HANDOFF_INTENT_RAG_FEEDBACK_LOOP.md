# 转交提示词：意图 RAG + 澄清沉淀闭环（含验收）

> 日期：2026-10-16  
> 依据：`TECH_LAYERED_INTENT.md` v1.2、`TECH_INTENT_RAG_IMPL.md`、`TECH_INTENT_FEEDBACK_LOOP.md`、`RESEARCH_INTENT_FEEDBACK_LOOP.md`  
> 仓库：主干 `aiVideo`（8443）  
> 用法：整段「复制给实现 Agent」粘贴即可；验收以本文 §验收标准 为准。

---

## 复制给实现 Agent

```text
你是 aiVideo Host「意图识别 + 例句沉淀闭环」实现 Agent。仓库以当前 worktree / 主干 aiVideo（8443）最新为准。中文注释与错误文案。做完在 README 指到 TECH_LAYERED_INTENT.md v1.2 与 TECH_INTENT_FEEDBACK_LOOP.md，并写清开关默认值。

# 依据（只读；冲突以较高版 / 本文锁定为准）
- TECH_LAYERED_INTENT.md v1.2
- TECH_INTENT_RAG_IMPL.md
- TECH_INTENT_FEEDBACK_LOOP.md
- 本文「已锁定决策」与「验收标准」；不要再问产品选型

# 总目标（两圈）
【在线】结构排除 → 强门闩 → exemplar embedding 定 L1/L2 → 槽位 L3 → 可执行性 → 弱则 clarify（点选或打字）→ plan_from_decision → 队列。
【离线】澄清定死 / 可解析打字 / 成功且未撤销 → 候选日志 → 清洗审校 → Approve 写入例句 → 重建向量 → shadow/canary → 线上更准。
P0 必须同时落地「在线主路径可切」+「候选可记、可洗、可入库脚本」；即梦式体验：拿不准就问，问完/跑成功的句子能沉淀，禁止一点选就自动灌库。

# 非目标
- 不重做队列 / CAS / 积分 / 能力目录骨架
- 不做 P2 真复合「先改图再视频」；不做在线每句改 embedding 权重
- 不声称对接即梦/可灵内部流水线
- 不改无关画布 UI（除 PR-6 指纹拒发）

# 已锁定决策（禁止再打开）
1. L1/L2 主路径 = exemplar embedding；正则只做门闩（否定 / 极强形式 / 入口冲突 / 挂起 clarify）。
2. Embedding：本机或内网侧车 HTTP；固定 embed_model_id；exemplar&lt;2k 暴力余弦即可；禁止默认上云 API。
3. 只在 possibleL2 内检索；视频能力关只写 executable.capabilityOk，不从 possibleL2 剔除视频意图。
4. 无关键词弱动态（活一点 / 动起来 / animate）：一律 clarify，不得静默选边。
5. embedding 失败/超时 → 门闩 + 兜底 + clarify，禁止默认视频。
6. 有图要视频默认 path=image_to_multiframe；一步 I2V 仅 feature + 明确触发。
7. 视频不可执行 → 明确失败，禁止静默降级生静图。
8. clarify 绑定原 request_id / 快照；选项回复不重跑检索；打字须解析出唯一 L2 才锁死。
9. L3 槽位不得改写 L2。
10. feature flag INTENT_RAG；切主路径（PR-5）必须在 PR-2+PR-3 绿之后。
11. 澄清点选 / 可解析打字 / 任务成功 = 候选标签，不是金牌；必须经清洗 + Approve 才进 exemplars；禁止 auto-ingest 全量点击。
12. 索引按批重建（脚本/定时），不做每条在线更新权重；新例句先 shadow 再全量。
13. 发送：capture → ensureSaved；仅「被选节点内容指纹变」拒发，换选区不拒发。

# 改造锚点（实现前再 rg 一遍）
- apps/api/backend/agent/media/request_context.py::resolve_context — 不再判目标类型
- apps/api/backend/agent/media/planner.py — plan_intent → plan_from_decision(decision, refs, catalog)
- apps/api/backend/agent/media/service.py::MediaHost.create — resolve 与 plan 之间插入 IntentPipeline
- apps/web/canvas/agent/store.ts::send — 节点指纹拒发
- 新建 apps/api/backend/agent/media/intent/（见下）

# 新建目录
apps/api/backend/agent/media/intent/
  __init__.py
  types.py              # IntentDecision, Feasibility, Certainty, ClarifyTicket
  feasibility.py
  gates.py
  exemplars.yaml        # 版本字段 rule_version / exemplars_version
  embedder.py
  retriever.py
  slots.py
  decide.py
  pipeline.py
  degrade.py
  feedback_log.py       # 写候选（clarify_click|clarify_text|task_success）
  wash.py               # 或 scripts/wash_exemplars.py：清洗规则
  promote.py            # Approve → 写入 yaml + 触发 reindex
  reindex.py            # 重建向量缓存

# 分 PR（按序；5 必须在 2+3 绿后；8 可与 3/7 并行）
PR-1 Feasibility + IntentDecision 类型与单测（不切流量）
PR-2 exemplars.yaml + embedder + retriever；阈值 T_strong / T_margin / T_min 先占位后用评测标定
PR-3 gates + decide + clarify ticket（点选 + 打字解析）；选项绑定快照
PR-4 slots L3 + executable（积分/限流/安全错误码区分）
PR-5 删除双正则主路径；INTENT_RAG=true 切流量
PR-6 前端：被选节点指纹变更拒发
PR-7 评测集 + Trace（embed_model_id、topK、exemplar_id、certainty、embed_degraded、rule_version、exemplars_version）+ CI
PR-8 闭环：feedback_log + wash 脚本 + Promote(Approve) + reindex + shadow 开关；候选表/日志字段完整

# Exemplar 与候选最小约定
- P0 每 L2 ≥15 中文 +5 英文；另加：弱动态、风格义「动画风」、否定视频、unsupported、问候 other
- 候选 provenance 必填：source、utterance、l2、request_id、snapshot_ref、ts、user_action
- 清洗：去噪、去歧义、近重复合并、标签冲突消解、可行性一致、hard negative、脱敏、防评测泄漏
- 入库：人工/脚本 Approve → yaml → reindex → 跑评测 → 升 exemplars_version；失败可回滚上一版本

# Trace（每次意图决策必打）
request_id, message_hash, possibleL2, topK[{id,l2,score}], certainty, decided_l2, path,
clarify_id?, embed_model_id, embed_degraded, rule_version, exemplars_version, gates_hit[]

# 完成定义
- INTENT_RAG 可开；双正则不再是主路径
- E01–E06 + 闭环 A01–A05 全绿（见下文验收）
- 原 C01/C10 / host_request_context 回归绿
- README 写清开关、例句版本、wash/promote 命令
```

---

## 验收标准

### A. 在线意图（必须全绿）

| ID | 场景 | 期望 | 失败判据 |
|----|------|------|----------|
| E01 | 「活一点 / 动起来」+ 选中图 | clarify（改图↔视频）或明确视频候选 | 静默文生图 / 静默改图且不告知 |
| E02 | 「动画风格海报」类 | 图路径 + style，不得视频 | 走出视频计划 |
| E03 | 「别做成视频，换成水彩」+ 选中图 | `image_edit` | 进视频或文生图 |
| E04 | `animate this image` + 选中图 | 视频族或 clarify | 直接当改图跑完 |
| E05 | embedding 超时/挂掉 | 降级可观测（Trace `embed_degraded=true`）；门闩+clarify/兜底 | 默认视频；无 Trace |
| E06 | 视频能力关 + 明确要视频 | 明确失败（capability） | 静默生静图冒充 |
| E07 | 「做成视频」强形式 + 有图 | 锁视频 path（默认 multiframe） | 被槽位改成改图 |
| E08 | 无有效图 + 「改成水彩」 | 不在 possibleL2 出现 image_edit；引导补图或文生图策略按产品默认 | 硬跑改图工具 |
| E09 | clarify 点「做成视频」 | 用**原**快照出视频计划；不重跑检索改口 | 换快照 / 重检索改 L2 |
| E10 | clarify 打字「改图」 | 解析锁 L2=改图；解析不出则继续问，不入库 | 解析失败仍开跑 |

### B. 回归（必须全绿）

| ID | 项 |
|----|----|
| R01 | 原 C01 / C10（或等价 host 回归）绿 |
| R02 | `host_request_context` / 选区与 @ 解析行为不回退 |
| R03 | INTENT_RAG=false 时旧路径仍可用（或文档写明已删除并仅新路径） |

### C. 闭环沉淀（必须全绿）

| ID | 场景 | 期望 |
|----|------|------|
| A01 | 澄清点选后 | 候选日志有一条：`source=clarify_click`，含原文+L2+request_id；**未 Approve 前** exemplars.yaml 不变 |
| A02 | 澄清打字可解析 | `source=clarify_text` 进候选；解析失败不进候选 |
| A03 | 任务成功且短时未撤销 | 可记 `source=task_success` 弱候选；默认不自动 Approve |
| A04 | wash + Approve | 去噪/近重复合并后写入 yaml；`exemplars_version` +1；reindex 后同款弱句可 strong 命中（用固定样例测） |
| A05 | 误点/对抗/空话 | wash 丢弃或进拒绝队列；不可进线上索引 |
| A06 | 回滚 | 能切回上一 `exemplars_version` 并恢复检索行为 |
| A07 | 评测泄漏 | 回归集句子不得未标记进入 exemplars |

### D. 可观测与工程（必须）

| ID | 项 |
|----|----|
| O01 | 每次决策 Trace 字段齐全（见提示词 Trace 列表） |
| O02 | CI：评测脚本对 E01–E06 固定集；失败阻断合入 |
| O03 | README：INTENT_RAG 默认、embed 侧车启动、`wash`/`promote`/`reindex` 命令、版本回滚步骤 |
| O04 | 高成本误判（改图↔视频）相对切流前基线不恶化（先录一版基线分母） |

### E. 明确不验收 / 禁止项

- 一点选就自动全量进线上例句库  
- embedding 挂了默认赌视频  
- 视频能力关时静默出图  
- 槽位或二次正则改写已锁 L2  
- 无审校的「成功任务」直接当金牌例句  

---

## 上线门（合并前 Checklist）

- [ ] PR-1～PR-8 按依赖合入；INTENT_RAG 可灰度  
- [ ] E01–E10、R01–R03、A01–A07、O01–O04 全绿  
- [ ] 基线误判率已记录；shadow 至少跑过一轮固定集  
- [ ] 例句版本与回滚演练做过一次  

---

## 一句话

按 v1.2 把意图主路径换成「查例句 + 拿不准就问」，并把问完/跑成功的句子洗进例句库；点选只是候选，Approve 后才进索引，视频绝不能再被正则或降级悄悄改成一张图。
