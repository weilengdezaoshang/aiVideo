# 转交提示词：分层意图 Embedding RAG（v1.2 P0）

## 复制给实现 Agent

```text
你是 aiVideo Host 意图层实现 Agent。仓库以当前 worktree / 主干 aiVideo（8443）最新为准。

# 依据（只读，冲突以较高版本为准）
- TECH_LAYERED_INTENT.md v1.2（产品锁定 + 流程）
- TECH_LAYERED_INTENT_IMPL.md / TECH_INTENT_RAG_IMPL.md（落地步骤）
- 本文锁定的部署与默认决策；不要再问产品选型

# 目标
把现网双正则「是否视频」主路径换成：结构排除 → 强门闩 → exemplar embedding 定 L1/L2 → 槽位 L3 → 可执行性 → 弱则 clarify → plan_from_decision。
P0 就要能处理口语弱动态、风格义、否定嵌套、英文 animate；视频贵，误判不能静默生图冒充视频，也不能默认赌视频。

# 非目标
不重做队列 / CAS / 积分骨架；不做 P1 重排与在线校准；不做 P2 真复合「先改图再视频」；不改无关画布 UI。

# 已锁定决策（禁止再打开讨论）
1. Embedding 部署：本机或内网侧车 HTTP；固定 embed_model_id；exemplar <2k 用暴力余弦即可。不要默认上云 API。
2. 无关键词弱动态（活一点 / 动起来 / animate）：一律 clarify（改图↔视频），不得静默选边。
3. L1/L2 主路径 = embedding；正则只做门闩（否定 / 极强形式 / 入口冲突 / 挂起 clarify）。
4. 只在 possibleL2 内检索；视频能力关只写 executable，不从 possibleL2 剔除视频意图。
5. embedding 失败 / 超时 → 门闩 + 兜底 + clarify，禁止默认视频。
6. 有图要视频默认 path=image_to_multiframe；一步 I2V 仅 feature + 明确触发。
7. 视频不可执行 → 明确失败，禁止降级生静图。
8. clarify 绑定原 request_id / 快照；选项回复不重跑检索。
9. L3 槽位不得改写 L2。
10. feature flag INTENT_RAG；切主路径（PR-5）必须在 PR-2+PR-3 绿之后。

# 仓库现状提示（核对快照，实现前再 rg 一遍）
- 本机工作区曾核对：无 sentence-transformers / 专用意图 embedding 依赖；openai 仅评测裁判方向。PR-2 需新增 embedder 与向量缓存。
- Host 路径同步 embedding：P0 允许同步调用，必须有超时预算 + degrade；延迟占位内测定，写入配置。
- 改造锚点（以现网文件为准）：
  - resolve_context：不再判目标类型，只做参考 / 选区 / 有效 image
  - plan_intent → plan_from_decision(decision, refs, catalog)
  - MediaHost.create：resolve 与 plan 之间插入 IntentPipeline
  - 前端 send：capture 后 ensureSaved，被选节点指纹变则拒发（不因换选区拒发）

# 新建目录
apps/api/backend/agent/media/intent/
  types.py feasibility.py gates.py exemplars.yaml embedder.py
  retriever.py slots.py decide.py pipeline.py degrade.py

# 分 PR（按序；1→2→3 可部分并行，5 必须最后）
PR-1 Feasibility + IntentDecision 类型与单测（不切流量）
PR-2 exemplars.yaml + embedder + retriever；阈值 T_strong/T_margin/T_min 先占位后评测标定
PR-3 gates + decide + clarify ticket
PR-4 slots L3 + executable（积分/限流/安全错误码区分）
PR-5 删除双正则主路径，切 INTENT_RAG
PR-6 前端节点指纹拒发
PR-7 评测集 + Trace（embed_model_id、topK、exemplar_id、certainty、embed_degraded）+ CI

# Exemplar 最小规模（P0）
每 L2 ≥15 中文 +5 英文；另加弱动态、风格义「动画风」、否定视频、unsupported、问候 other。
入库：错例 → 人工标 → yaml → 重嵌 → 跑评测 → 升 rule_version。

# 验收门（必须）
- E01 活一点 + 选中图 → clarify 或视频候选，不得静默文生图
- E02 动画风格海报 → 图路径 + style，不得视频
- E03 别做成视频换水彩 + 选中图 → image_edit
- E04 animate this image + 选中图 → 视频族 / clarify，不得直接改图
- E05 embedding 超时 → 降级可观测，不得默认视频
- E06 视频能力关 + 明确要视频 → 明确失败，不改图
- 原 C01/C10 / host_request_context 回归绿
- 高成本误判相对基线不恶化（先录一版基线）

# 默认工程约定
中文注释与错误文案；最小侵入接 Host；阈值禁止拍脑袋写死不测；Trace 必带 rule_version 与 embed_model_id。
做完在 README 指到 TECH_LAYERED_INTENT.md v1.2，并写清 INTENT_RAG 开关默认。
```

## 一句话
按 v1.2 把意图主路径换成 exemplar embedding + 门闩，弱动态一律问，视频绝不能再被正则或降级悄悄改成一张图。

## 本机推进记录（2026-10-16）
- 产品待确认项 P9/P10 已按方案推荐默认锁定进本稿，便于直接开写。
- C9：box 上 aivideo 快照未见可复用意图 embedding 依赖（实现前以你本机最新仓再确认一次）。
