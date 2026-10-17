# 实现方案：分层意图（RAG exemplar + 槽位 + 门禁）v1.2 落地步骤

> 日期：2026-10-16  
> 依据：`TECH_LAYERED_INTENT.md` v1.2  
> 仓库：主干 `aiVideo`（8443）  
> 目标：替换现网「双正则词表」为主路径，上线 **RAG 判意图 + 槽位填参 + 硬门禁**；只改意图与接入，不重做队列/CAS 骨架

---

## 0. 一句话

先结构排除 → 强门闩 → exemplar 向量检索定 L1/L2 → 槽位填 L3 → 可执行性检查 → 弱则澄清 → 原有 `plan_intent` 模板执行。

---

## 1. 现状要动哪些点（已核对）

| 模块 | 路径 | 现状 | 改造 |
|------|------|------|------|
| 参考解析 | `apps/api/backend/agent/media/request_context.py::resolve_context` | 自有 video/edit 正则 | **不再判目标类型**；只做 ID 重读、@/选区来源、有效 image |
| 计划 | `apps/api/backend/agent/media/planner.py::plan_intent` | 另一套 is_video 正则出模板 | 改为 **吃 IntentDecision**，只按 path 填模板 |
| 入口 | `apps/api/backend/agent/media/service.py::MediaHost.create` | resolve → plan_intent | 中间插入 IntentPipeline |
| 前端发送 | `apps/web/canvas/agent/store.ts::send` | 先 capture 再 ensureSaved | 补「被选节点保存期变更拒发」 |
| 协议 | `apps/web/shared/agent-protocol.ts` | 已有 AgentRequest | 可扩展 intent 调试字段（可选） |

---

## 2. 目标架构

```
HostTurn / AgentRequest
    → SnapshotGate（① 前端 + ② 后端节点校验）
    → FeasibilityBuilder（③ possibleL2 + capability 标记）
    → RuleGates（④′ 否定/强形式/挂起态）
    → EmbeddingRetriever（④⑤ exemplar RAG）
    → SlotFiller（⑥ L3）
    → ExecutableChecker（⑥′ 积分等）
    → DecideOrClarify（⑦）
    → plan_from_decision（⑧ 原模板）
    → admit / queue / CAS（⑨）
```

新建建议目录：

```
apps/api/backend/agent/media/intent/
  __init__.py
  types.py              # IntentDecision, Feasibility, Certainty
  feasibility.py
  gates.py              # 强规则门闩
  exemplars.yaml        # 或 jsonl；例句库
  embedder.py           # 向量接口
  retriever.py          # 只在 possibleL2 内 topK
  slots.py
  decide.py
  pipeline.py           # 编排
  degrade.py            # embedding 失败降级
```

---

## 3. 分 PR 实施步骤（推荐顺序）

### PR-1：类型与 Feasibility（不切流量）

1. 增加 `IntentDecision` / `Feasibility`（`possibleL2` + `executable`）。  
2. 从选区、@、入口、节点 kind 计算 `possibleL2`：  
   - 无有效 image → 去掉 `image_edit` / `image_to_*`  
   - 视频能力开关只写 `executable.capabilityOk`，**不**从 possibleL2 剔除视频  
3. 单测：无图、有图、选中视频节点、@ 优先。  
4. **验收**：旧 `plan_intent` 仍跑；新模块仅单测。

### PR-2：Exemplar 库 + Embedding 检索

1. 建 `exemplars.yaml`（中英），按 L2 分组，至少覆盖：改图、文生图、文生视频、有图做视频、弱动态、风格义「动画风」、否定视频、问候、unsupported。  
2. `embedder.py`：固定 `embed_model_id`；启动或 CI 预计算 exemplar 向量缓存。  
3. `retriever.py`：`query = message + 结构标签`；过滤 `l2 ∉ possibleL2`；返回 topK score。  
4. 分数→`certainty`：阈值配置 `[T_strong]/[T_margin]/[T_min]`，先占位后用评测标定。  
5. **验收**：离线脚本对评测句出 top1；E02/E04 类用例方向正确。

**选型（待确认，推荐）**：本机/内网 sentence-transformers 或多语 API；exemplar&lt;2k 条用暴力余弦即可。

### PR-3：门闩 + 决策 + 澄清

1. `gates.py`：挂起 clarify 短路；强否定视频；强「做成视频」；入口↔文字硬冲突 → clarify。  
2. `decide.py`：strong 执行；weak 且改图↔视频 → clarify；none → 兜底表（有图默认改图，无图默认文生图，问候 other）。  
3. 接入 `service.py`：`intent_clarify` 写 ticket（TTL、绑定 request_id/快照）；选项回复**不**重跑检索。  
4. **验收**：M02/E01 会问；点「做成视频」绑定原快照出视频计划。

### PR-4：槽位 L3 + 可执行性

1. `slots.py`：抽 style、duration_sec、ref_role（默认首帧）、tier=preview。  
2. **禁止**槽位改写 L2。  
3. L3 之后查积分/限流/内容安全；错误码区分。  
4. **验收**：「水彩风 5 秒」→ 视频 + style + duration；能力关 → 失败不生静图。

### PR-5：砍掉双正则主路径

1. `resolve_context` 删除目标类型正则，只保留参考解析。  
2. `plan_intent(message, refs, …)` 改为 `plan_from_decision(decision, refs, catalog)`。  
3. 有图视频默认 `image_to_multiframe`；一步 I2V 仅 feature+明确触发。  
4. **验收**：原 C01/C10/host_request_context 全绿；animate 不再串改图。

### PR-6：前端发送一致性补强

1. 点击瞬间记录选区 + 被选节点指纹（assetId/assetVersion）。  
2. `ensureSaved` 后比对节点指纹，变则拒发。  
3. 不因用户换选区拒发。  
4. **验收**：canvas-request-context 单测补节点变更用例。

### PR-7：评测、埋点、配置

1. 评测集 JSON：期望 L1/L2/行为；盲测集隔离。  
2. Trace 落库：`rule_version`、`embed_model_id`、topK、exemplar_id、certainty、clarify 选择。  
3. embedding 超时 → degrade（门闩+兜底+clarify），打点 `embed_degraded`。  
4. **验收**：CI 跑固定集；高成本误判相对基线不恶化（基线先录一版）。

### PR-8（可选 P0 末）：无 context 收口

确认无外部旧端后：无 `context` → 400 `CONTEXT_REQUIRED`；删静默 `selectedObjectIds` 主路径。

---

## 4. 单次请求伪代码

```python
def run_intent(request, doc, catalog):
    if pending_clarify(request):
        return bind_clarify(request)  # 不重识别

    feas = build_feasibility(request, doc, catalog)
    gated = apply_gates(request.message, feas, entry_mode=...)
    if gated.short_circuit:
        return gated.decision

    try:
        hits = retrieve(embed(query_text(request, feas)), allow=feas.possibleL2)
        decision = decide_from_hits(hits, feas, gates=gated)
    except EmbedError:
        decision = degrade(request, feas, gated)  # 保守，不赌视频

    decision.l3 = fill_slots(request.message, decision.l2)
    check_executable(decision, catalog)  # 或 raise 区分错误
    if decision.needsClarify:
        return admit_clarify(decision)
    plan = plan_from_decision(decision, refs=..., catalog=catalog)
    validate_plan(plan, catalog)
    return admit(plan)
```

---

## 5. Exemplar 库最小规模（P0）

每个 L2 ≥ 15 条中文 + 5 条英文；另加：

- 弱动态：「活一点」「动起来」「animate this」  
- 风格义：「动画风格」「anime style poster」  
- 否定：「不要视频只要水彩」  
- unsupported：「把视频改成水彩」「对齐这些图层」  
- other：「你好」「怎么用」

入库流程：错例 → 人工标 → 追加 yaml → 重嵌向量 → 跑评测 → 升 `rule_version`。

---

## 6. 槽位表（L3）

| 槽 | 来源 | 默认 |
|----|------|------|
| style | 抽取/exemplar 提示 | 空 |
| duration_sec | 「N 秒」 | preview 短时长（如目录允许值） |
| ref_role | 有图做视频 | first_frame |
| tier | 系统 | preview |

---

## 7. 风险与降级

| 风险 | 处理 |
|------|------|
| embedding 慢/挂 | 超时预算；降级 clarify/兜底；不默认视频 |
| 例句不够 | 先 clarify 多一点；靠回流补库 |
| 与旧正则并存期 | feature flag `INTENT_RAG=1`；默认开后删旧 is_video |
| 阈值不准 | 只用评测标定；禁止拍脑袋写死后不测 |

---

## 8. 验收清单（上线门）

- [ ] 选中图+水彩 → 改图  
- [ ] 选中图+动起来/animate → 澄清或视频，不直接改图  
- [ ] 动画风格海报 → 非视频  
- [ ] 否定视频 → 改图  
- [ ] 无选区+画猫 → 文生图  
- [ ] 要视频但能力关 → 明确失败  
- [ ] embedding 超时 → 降级可观测  
- [ ] clarify 选项绑定原快照  
- [ ] C01/C10 与 host_request_context 回归绿  
- [ ] 节点保存期被改 → 拒发  

---

## 9. 工作量观感（供排期，非承诺）

| PR | 内容 | 大致量级 |
|----|------|----------|
| 1 | Feasibility | 小 |
| 2 | Embedding+库 | 中 |
| 3 | 门闩+澄清 | 中 |
| 4 | 槽位+可执行 | 小 |
| 5 | 切主路径 | 中（回归重） |
| 6 | 前端指纹 | 小 |
| 7 | 评测埋点 | 中 |

可 1→2→3 并行后半与 6；**5 必须在 2+3 绿后**。

---

## 10. 默认决策

1. P0 就上 RAG，不等「先正则后模型」。  
2. 只在 possibleL2 内检索。  
3. 弱分改图↔视频必问。  
4. 失败降级保守。  
5. `plan_intent` 不再维护 is_video 词表。

---

## 11. 下一步

确认 embedding 部署（本机/侧车）后，可出复制即用的 `HANDOFF_INTENT_RAG.md` 给实现 Agent。
