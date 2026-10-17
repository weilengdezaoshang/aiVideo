# 转交提示词：P0 意图 RAG 闭环 + P2 顺位多步接入（含验收与案例）

> 日期：2026-10-16  
> 依据：`TECH_LAYERED_INTENT.md` v1.2、`TECH_INTENT_RAG_IMPL.md`、`TECH_INTENT_FEEDBACK_LOOP.md`、`HANDOFF_INTENT_RAG_FEEDBACK_LOOP.md`  
> 仓库：主干 `aiVideo`（8443）  
> 用法：整段「复制给实现 Agent」粘贴即可；验收以本文 §验收标准 为准。  
> 独立案例册（产品审）：`CASES_INTENT_SEQUENTIAL.md`

---

## 0. 一句话

**P0**：查例句定「一件事」的 L2，拿不准就问，Approve 才进例句库。  
**P2**：把「先 A 再 B」解析成有序 `steps[]`，仍走现有 `plan_from_decision` → Executor，不新开执行通道；开关默认关。

---

## 复制给实现 Agent

```text
你是 aiVideo Host「意图识别（P0 RAG+闭环）+ 顺位多步（P2）」实现 Agent。仓库以当前 worktree / 主干 aiVideo（8443）最新为准。中文注释与错误文案。做完在 README 指到 TECH_LAYERED_INTENT.md v1.2、HANDOFF_INTENT_RAG_FEEDBACK_LOOP.md、本文，并写清开关默认值。

# 依据（只读；冲突以较高版 / 本文锁定为准）
- TECH_LAYERED_INTENT.md v1.2
- TECH_INTENT_RAG_IMPL.md
- TECH_INTENT_FEEDBACK_LOOP.md
- HANDOFF_INTENT_RAG_FEEDBACK_LOOP.md（P0 在线+闭环；本文不重复推翻）
- 本文「已锁定决策」与「验收标准」；不要再问产品选型

# 分期目标
【P0 — 必须先绿】结构排除 → 强门闩 → exemplar embedding 定 L1/L2 → 槽位 L3 → 可执行性 → 弱则 clarify → plan_from_decision（单步）→ 队列。
  离线：澄清定死 / 可解析打字 / 成功未撤销 → 候选 → 清洗 → Approve → reindex。
  IntentDecision 视为 steps 长度恒为 1。
【P1 — 可选并行】例句周更、阈值标定、hard negative、影子评测；仍单步。
【P2 — 本文增量】仅当 feature INTENT_MULTI_STEP=true 且用户确认/强顺位命中时：产出 steps[]（≤2）→ Planner 每步 plan_from_decision 拼 DAG → Executor 按序跑。
  默认 INTENT_MULTI_STEP=false：顺位句走 clarify / 拆步，行为与 P0 一致。

# 非目标
- 不重做队列 / CAS / 积分 / 能力目录骨架
- P0/P1 不做真复合自动连跑；P2 不做任意长度规划、不做开放式工具调用
- 不声称对接即梦/可灵内部流水线
- 不改无关画布 UI（除既有 PR-6 指纹拒发）
- 不一点选就 auto-ingest 例句库

# 已锁定决策（禁止再打开）
## 继承 P0（见 HANDOFF_INTENT_RAG_FEEDBACK_LOOP.md，摘要）
1. L1/L2 主路径 = exemplar embedding；正则只做门闩。
2. Embedding：本机或内网侧车；固定 embed_model_id；exemplar<2k 暴力余弦；禁止默认上云。
3. 只在 possibleL2 内检索；视频能力关只写 executable.capabilityOk。
4. 无关键词弱动态：一律 clarify。
5. embedding 失败 → 门闩+兜底+clarify，禁止默认视频。
6. 有图要视频默认 path=image_to_multiframe；一步 I2V 仅 feature+明确触发。
7. 视频不可执行 → 明确失败，禁止静默降级静图。
8. clarify 绑原 request_id/快照；选项不重跑检索；打字须唯一 L2。
9. L3 槽位不得改写 L2（P2：不得改写该步 L2）。
10. INTENT_RAG；切主路径须 PR-2+PR-3 绿后。
11. 点选/打字/成功 = 候选；Approve 才进 exemplars。
12. 索引按批重建；shadow 再全量。
13. 发送：仅被选节点内容指纹变拒发。

## P2 增量锁定
14. feature flag INTENT_MULTI_STEP 默认 false。
15. 白名单顺位（仅此三种，顺序固定）：
    a) image_edit → image_to_multiframe
    b) image_edit（含抠图/去背景语义）→ image_to_multiframe
    c) text_to_image → image_to_multiframe
    其它组合（视频→改图、三步、先视频再抠图等）→ clarify 或 unsupported，禁止硬跑。
16. 单次决策 max_steps=2；超长顺位拆成多次用户确认，不自动展开。
17. 每步独立：possibleL2 / slots / capability / 积分检查；步间用上一步产物节点 id 或 URL 作下一步 refs。
18. Planner：对 steps[i] 调用既有 plan_from_decision，再组装为有序计划（depends_on）；禁止意图层直接调工具。
19. Executor：严格按序；任一步失败则停，不静默跳过；过程面板分步 todo。
20. P0 关开关时：命中顺位 exemplar（multi_step_hint）→ clarify「先做 A 还是先做 B / 是否连跑」，不得自动 steps[]。
21. 开开关且强顺位（「先…再…」+ 白名单）→ 可直接 steps[]；弱顺位仍 clarify 确认后再锁 steps。
22. 复合句「水彩做成视频」若无先后词：P0/P2 均可定为单步视频+style（非真顺位）；真顺位必须有先后语义或用户确认两步。

# 改造锚点（实现前再 rg）
- apps/api/backend/agent/media/request_context.py::resolve_context
- apps/api/backend/agent/media/planner.py — plan_from_decision；P2 增加 plan_from_steps(decision)
- apps/api/backend/agent/media/service.py::MediaHost.create — IntentPipeline
- apps/api/backend/agent/media/intent/types.py — IntentDecision.steps?: Step[]
- Executor / 队列消费处：按 plan.steps 或 depends_on 串行
- apps/web 过程面板：多步 todo（可选与 PR-12 同 PR）

# 类型增量（P2）
Step = {
  index: int,                 # 0-based
  l2: L2,
  path: str,                  # 与现网 path 一致
  slots: dict,                # 本步 L3；不得改 l2
  refs_policy: "request"|"prev_output",
  depends_on?: int,           # 上一步 index
}
IntentDecision.steps: Step[]  # P0 恒 len=1；P2 ≤2
IntentDecision.multi_step: bool
IntentDecision.needsClarify / clarify_options 仍可用

# 分 PR
## 已有 P0：PR-1…PR-8（见 HANDOFF_INTENT_RAG_FEEDBACK_LOOP.md）— 必须先绿
## P2 增量：
PR-9  types：Step / IntentDecision.steps；单测序列化；INTENT_MULTI_STEP 开关只读默认 false
PR-10 顺位检测：门闩/exemplar 标签 multi_step_hint → 解析有序 L2 对；非白名单 → clarify/unsupported；单测 S01–S04
PR-11 Planner：plan_from_steps → 多次 plan_from_decision + depends_on；Executor 按序；失败停；能力关逐步检查
PR-12 过程 UI 分步 todo + Trace（steps、step_index、INTENT_MULTI_STEP）；评测集 Q 用例；开关文档

# Trace 增量（P2）
在 P0 Trace 上增加：intent_multi_step_enabled, multi_step, steps[{l2,path,slots_digest,depends_on}], step_fail_index?

# 完成定义
- P0：HANDOFF_INTENT_RAG_FEEDBACK_LOOP 的 E/A/R/O 全绿
- P2：INTENT_MULTI_STEP=false 时 Q 用例行为= P0（clarify/单步）；=true 时 S01–S08 全绿
- 白名单外顺位不硬跑；max 2 步；README 写清两开关与回滚
```

---

## 1. 架构：P0 单步 vs P2 多步

```
HostTurn
  → Feasibility / Gates / RAG / Slots / Executable   （同 P0）
  → Decide
       │
       ├─ 单步（默认）→ IntentDecision { l2, path, slots }  ≡ steps=[一步]
       │                    → plan_from_decision → Executor
       │
       └─ 顺位且 INTENT_MULTI_STEP
              ├─ false → clarify / 拆步文案（不生成多步计划）
              └─ true  + 白名单 +（强形式|用户确认）
                         → IntentDecision.steps[≤2]
                         → plan_from_steps
                              · for each step: plan_from_decision(step)
                              · 串联 depends_on / prev_output
                         → Executor 按序跑
```

**原则**：意图层只产出决策与步骤表；**Planner 填模板，Executor 跑白名单工具**——与现网一致，只是把「一次决策」扩成「有序多次决策」。

---

## 2. 白名单与拒绝

| 允许（P2 开） | 示例 |
|---------------|------|
| `image_edit` → `image_to_multiframe` | 先改水彩，再做成视频 |
| `image_edit`（抠图）→ `image_to_multiframe` | 先抠图去背景，再动起来 |
| `text_to_image` → `image_to_multiframe` | 先画一只猫，再生成 5 秒视频 |

| 拒绝 / clarify | 原因 |
|----------------|------|
| 三步及以上 | max_steps=2 |
| 视频 → 改图 | 非白名单 |
| 两步都是视频 | 非白名单 |
| 无图却要 edit→video | 第一步可行性失败 |
| 开关关闭时的顺位句 | 只澄清，不连跑 |

---

## 3. JSON 样例（Q-01）

用户（有选中图）：「先抠图去背景，再做成 5 秒视频」  
`INTENT_MULTI_STEP=true`，用户已确认或强顺位命中：

```json
{
  "request_id": "req_q01_demo",
  "certainty": "strong",
  "multi_step": true,
  "needsClarify": false,
  "l2": "image_edit",
  "path": "image_edit",
  "steps": [
    {
      "index": 0,
      "l2": "image_edit",
      "path": "image_edit",
      "slots": {
        "op": "matting",
        "background": "transparent"
      },
      "refs_policy": "request",
      "depends_on": null
    },
    {
      "index": 1,
      "l2": "image_to_multiframe",
      "path": "image_to_multiframe",
      "slots": {
        "duration_sec": 5,
        "ref_role": "first_frame",
        "tier": "preview"
      },
      "refs_policy": "prev_output",
      "depends_on": 0
    }
  ],
  "executable": {
    "capabilityOk": true
  },
  "trace": {
    "intent_multi_step_enabled": true,
    "exemplars_version": "0.2.0",
    "rule_version": "p2.0"
  }
}
```

`INTENT_MULTI_STEP=false` 时同句应类似：

```json
{
  "needsClarify": true,
  "multi_step": false,
  "clarify_options": [
    {"id": "step_edit_only", "label": "先只抠图", "l2": "image_edit"},
    {"id": "step_video_only", "label": "直接做视频（用当前图）", "l2": "image_to_multiframe"},
    {"id": "ask_enable_multi", "label": "两步都做（需开启多步）", "disabled": true}
  ],
  "steps": null
}
```

---

## 4. 验收标准

### A. 继承 P0（必须仍绿）

以 `HANDOFF_INTENT_RAG_FEEDBACK_LOOP.md` 的 **E01–E10、A01–A07、R01–R03、O01–O04** 为准；P2 合入后不得回退。

### B. 顺位多步 S01–S08（P2）

| ID | 场景 | 期望 | 失败判据 |
|----|------|------|----------|
| S01 | 开关 **false** +「先抠图再做成视频」+ 有图 | clarify 或单步引导；**不**产生 len(steps)>1 的可执行计划 | 自动连跑两步 |
| S02 | 开关 **true** + 同句 + 有图 | `steps=[image_edit, image_to_multiframe]`；第二步 refs=上一步产物 | 单步吞掉；或第二步仍用原图且未声明 |
| S03 | 开关 true +「先画一只猫再做成视频」+ **无图** | `text_to_image → image_to_multiframe` | 第一步改成 edit；或无图硬 edit |
| S04 | 开关 true +「先做成视频再改水彩」 | **非白名单** → clarify 或 unsupported；不执行 | 强行视频→改图 |
| S05 | 开关 true + 三步口述「先抠再水彩再视频」 | 最多 2 步或澄清拆分；不自动 3 步 | 跑出 3 步计划 |
| S06 | 第二步视频能力关 | 第一步可成功或整体在可执行检查阶段明确失败；**禁止**第二步静默改静图冒充 | 静默降级 |
| S07 | 第一步工具失败 | Executor 停止；不跑第二步；Trace `step_fail_index=0` | 跳过失败继续 |
| S08 | 「改成水彩做成视频」（无「先/再」） | 单步视频+style **或** clarify；不算强制两步 | 无确认却拆成 edit→video |

### C. 回归

| ID | 项 |
|----|----|
| R-P2-1 | INTENT_MULTI_STEP=false 时全套 P0 评测不变绿 |
| R-P2-2 | 单步 C/W 用例不被 P2 代码误拆成多步 |
| R-P2-3 | 过程面板多步展示不影响单步 UX |

### D. 禁止项

- 开关默认 true  
- 白名单外组合硬跑  
- 意图层绕过 Planner 直接调生成工具  
- 槽位或「顺位补丁」改写某步已锁 L2  
- 多步成功自动 Approve 进例句（仍走候选+人工）

---

## 5. 案例表（实现/评测用）

完整口语扩充见 `CASES_INTENT_SEQUENTIAL.md`。此处为门禁摘要。

### 5.1 单步复杂 C（P0=P2，都是一件事）

| ID | 用户话 | expected_p0 | expected_p2 | notes |
|----|--------|-------------|-------------|-------|
| C-01 | 把衣服换成红的，整体水彩风 | image_edit + slots | 同左 | 多约束同 L2 |
| C-02 | 去背景，透明底，保留发丝 | image_edit (matting) | 同左 | 抠图 |
| C-03 | 别做成视频，换成油画 | image_edit | 同左 | 否定视频 |
| C-04 | 动画风格海报 | image_edit/t2i + style | 同左 | 风格≠视频 |
| C-05 | 5 秒赛博朋克视频（有图） | image_to_multiframe + duration+style | 同左 | 单步视频 |
| C-06 | 画一只橘猫水彩风（无图） | text_to_image | 同左 | |
| C-07 | 不要背景只要人物抠出来 | image_edit (matting) | 同左 | |
| C-08 | remove background and keep hair detail | image_edit | 同左 | 英文抠图 |

### 5.2 弱动态 W（应澄清）

| ID | 用户话 | expected_p0 | expected_p2 | notes |
|----|--------|-------------|-------------|-------|
| W-01 | 动起来 | clarify 改图↔视频 | 同左（不因 P2 变多步） | |
| W-02 | 活一点 | clarify | 同左 | |
| W-03 | animate this | clarify 或视频族 | 同左 | |
| W-04 | 让人物稍微动一动 | clarify | 同左 | |
| W-05 | 有点动态的感觉 | clarify | 同左 | 极弱 |

### 5.3 顺位 Q（P0 vs P2 分水岭）

| ID | 用户话 | expected_p0 | expected_p2（开关 true） | notes |
|----|--------|-------------|-------------------------|-------|
| Q-01 | 先抠图去背景，再做成 5 秒视频 | clarify / 拆步 | edit(matting)→multiframe | JSON 见 §3 |
| Q-02 | 先改成水彩，再做成视频 | clarify | edit→multiframe | |
| Q-03 | 先画一只猫，再生成视频 | clarify 或引导 | t2i→multiframe | 无图 |
| Q-04 | 改完油画再动起来 | clarify | edit→multiframe 或仍 clarify「动起来」 | 第二段弱动态可再确认 |
| Q-05 | 先做成视频再改水彩 | clarify/unsupported | **拒绝连跑** | 非白名单 |
| Q-06 | 先抠图，再水彩，再视频 | clarify 拆分 | ≤2 步或澄清 | |
| Q-07 | 抠完透明底然后 animate | clarify | edit→multiframe | |
| Q-08 | first cut out the subject, then make a 5s video | clarify | edit→multiframe | 英文 |

---

## 6. 上线门 Checklist

- [ ] P0 PR-1…8 与 E/A/R/O 全绿  
- [ ] PR-9…12 合入；`INTENT_MULTI_STEP` 默认 false  
- [ ] S01–S08、R-P2-1…3 全绿  
- [ ] Q-01 在开关 true 下 Trace 可见 steps；开关 false 下无多步计划  
- [ ] README：两开关、白名单、失败停步、回滚  
- [ ] 案例册 `CASES_INTENT_SEQUENTIAL.md` 与评测 JSON 对齐  

---

## 7. 文档关系

| 文档 | 作用 |
|------|------|
| `HANDOFF_INTENT_RAG_FEEDBACK_LOOP.md` | P0 在线 + Approve 闭环 |
| **本文** | P0 继承 + P2 顺位接入提示词与验收 |
| `CASES_INTENT_SEQUENTIAL.md` | 产品可审的案例册 |
| `TECH_LAYERED_INTENT.md` v1.2 | 分层意图总纲 |
| `exemplars/exemplars.yaml` | 例句库（含 multi_step_hint / 抠图） |

---

## 8. 一句话交付

先保证 P0「单步 RAG + 问不清就问 + Approve 入库」；P2 只加开关控制的 `steps[]≤2` 白名单，经 `plan_from_decision` 串进现有 Executor——关开关时顺位句只能澄清，绝不能偷偷连跑。
