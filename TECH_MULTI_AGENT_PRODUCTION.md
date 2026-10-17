# 多 Agent 机制 — 可上线生产规格（完整版）
项	值
产品	aiVideo 无限画布创作助手（8443 / Host-only）
文档	TECH_MULTI_AGENT_PRODUCTION.md
版本	v1 production
日期	2026-10-13（Asia/Shanghai）
读者	实现工程师 / 平台 / 前端 / QA
取代意图	生产落地以本文为准；TECH_MULTI_AGENT_REDESIGN.md v2 为设计演进稿；能力门禁以 TECH_CAPABILITY_CATALOG.md v2 为准并在此对接
标记约定

MUST：上线前必须实现；违反即为缺陷。
SHOULD：强烈建议；缺省有明确降级路径与错误码。
MAY：可选增强，不阻塞上线。
1. 目标与非目标
1.1 目标（MUST）
对外 Host-only：用户只与一个「创作助手」对话；不并排多个 Agent 聊天窗。
对内可编排：Intent → Planner（Todo DAG）→ PlanValidator → Scheduler（按依赖串/并行）→ Worker → Gateway → Catalog/Adapter；可选 Reviewer。
意图自动路由：按用户话 + 画布参考自动选 capabilityId；无用户侧能力/档位设置。
默认样片再正片：系统默认 tier=preview；用户点「生成正片」再跑 tier=final（推荐新 run，引用 preview 资产）。
产物在画布：样片/正片落画布节点；右侧显示「进程 n/m」+ 折叠「处理过程」+ 确认按钮。
可并行才并行：四方向生图等无依赖、不写同一节点的步骤并行；视频主链串行。
Fail-closed：未开通能力、计划非法、合规拦截 → 显式错误；禁止视频静默改生图、prod 静默 mock。
可计费可对账可恢复：幂等、预占积分、取消回写、杀进程续看、错误码可分。
原 redesign §12 缺口全部有生产设计（改口、多 run、节点锁、配额半扇出、取消仍计费、合规红态、续跑、Reviewer soft/hard 等）——可分期实现，但契约与状态机 MUST 先落地，撞上场景只许显式失败，不许静默顶替。
1.2 非目标
画布上多个并列聊天 Agent / 用户手动勾选并行。
用并行加速有依赖的视频链。
扫全量百炼模型进表；一次打通所有禁用高级编辑按钮。
将 Redis 替换当前 Postgres Host 队列列为本文前置（队列实现可演进，契约不变）。
音频生成能力（未承诺则不进目录）。
1.3 与现有文档关系
文档	关系
TECH_MULTI_AGENT_REDESIGN.md v2	产品决策与缺口清单来源；生产细节以本文为准
TECH_CAPABILITY_CATALOG.md v2	能力目录 / binding / fail-closed；本文 §9 对接
TECH_INTENT_VIDEO_ROUTING.md	Intent path 与模板；本文 §5 吸收并统一到 capability 层
PROD_AGENT_ARCHITECTURE.md	业界对照；本文附录简述
2. 系统上下文与分层
用户 ──► L0 UI（画布 + 右侧助手）
              │ parts: plan / process / confirm / text / error
              ▼
         L1 Host（唯一对外作答）
              │
              ├─► Intent（结构化路由）
              ├─► Planner（Todo DAG）
              ├─► PlanValidator
              ├─► Scheduler（拓扑 / 并行 / 锁 / 预占）
              │       │
              │       ▼
              │   Worker(s) ──► L2 Tool Gateway
              │                      │
              │                      ▼
              │                 L5 Catalog + Binding
              │                      │
              │                      ▼
              │                 L3 Queue ──► L4 Adapter（百炼等）
              │                      │
              │                      ▼
              │                 画布节点 / 资产存储
              │
              └─► Reviewer（可选，审产物）
              │
              ▼
         Obs（trace / 计费流水 / 告警）

组件	层	职责	禁止
Host	L1	对话、发起规划、展示进度、HITL、汇总失败	写死供应商模型名；静默换能力
Intent	L1	话+参考 → capability 倾向 / clarify	做成设置面板
Planner	L1	输出带校验字段的 Todo DAG	规划 disabled 能力当成功
PlanValidator	L1	计划合法；打回重规划或失败	放过成环/抢节点/视频→单图结束
Scheduler	L1/runtime	分层执行、fanOut、锁、预占、取消、恢复	忽略节点锁并行写同一节点
Worker	L1/runtime	执行单步：调 Gateway、写节点、上报事件	绕过 Gateway / 持 Key
Tool Gateway	L2	目录校验、规格、报价预占、enqueue	未开通降级别的能力
Catalog/Binding	L5	能力菜单 + 部署绑定版本	Key 明文进仓/前端
Adapter	L4	平台协议 create/poll/cancel/mapError	被 Agent 直调；产品路由逻辑
Reviewer	L1	审产物（硬检查+内容）；soft/hard	未启用假装已审；审计划（计划归 Validator）
UI	L0	进程外露、过程折叠、样片画布	能力/档位设置；侧栏大播放器刷 JSON
Obs	cross	trace、流水、SLO、告警	缺 bindingVersion / providerTaskId
3. 角色与信任边界
3.1 信任边界（MUST）
边界	规则
用户 ↔ Host	用户只发自然语言与画布选择；不传模型 ID、不传 Key
Host ↔ Intent/Planner	结构化 JSON；温度低；输出经 Validator
Worker ↔ Gateway	仅 capabilityId + tier + args + run/step 上下文
Gateway ↔ Adapter	NormalizedJob；凭据仅 credentialRef 在 Worker/服务端解析
前端	只收 parts 与「能力是否可用 + 允许规格」；永不持有 Key / 内部 model 细节（对客户端）
Prompt 注入	用户话与画布元数据进隔离通道；工具参数经 Gateway schema 校验，禁止把用户原文当 shell
3.2 可见性
角色	用户可见？	UI 表现
Host	是	聊天文案、按钮
Intent / Planner / Scheduler / Worker	否	仅「进程」与折叠过程
Reviewer	否	过程内可有一行质检摘要；失败 soft 打标 / hard 阻断
3.3 实现形态（MUST 语义 / MAY 进程）
语义 MUST：agents-as-tools（Host 保留作答权）；禁止产品级多对话框。
进程 MAY：Planner/Reviewer 同进程函数或独立 Agent 定义；对外契约不变。
4. 端到端状态机
4.1 Run 状态（MUST）
idle
  → routing                 # Intent
  → clarifying?             # need_clarify，挂起等人答
  → planning                # Planner
  → validating              # PlanValidator（可回 planning，限次）
  → awaiting_confirm?       # 高积分 / 策略要求确认计划或报价（preview 默认可 auto）
  → reserving               # 积分预占（整 run 或按层）
  → scheduling
  → running                 # 一层或多层执行中
  → reviewing?              # Reviewer
  → awaiting_user_final     # preview 成功落画布，等人「生成正片/换个效果」
  → done
  → failed
  → canceling → cancelled
  → blocked                 # 合规等硬拦，进程红态，不落违规资产

转移要点（MUST）

从	到	条件
routing	clarifying	path=clarify 或置信度低
validating	planning	校验失败且 replanCount < MAX_REPLAN（建议 2）
validating	failed	超限或不可修复（如能力永久 unavailable）
running	canceling	用户改口/取消/新 run 抢同画布策略取消旧 run
running	blocked	content_blocked 且策略 hard
running	reviewing	步骤/层完成且挂载了 Reviewer
reviewing	running	soft 失败且允许重试该 step
reviewing	awaiting_user_final / done	preview 结束或 final 通过
*	canceling	显式 cancel；canceling 等子任务与 provider 终态
canceling	cancelled	未完成子任务已 cancel 或超时标记 cancel_timeout；流水保留 charged
4.2 Step 状态（MUST）
pending → ready → reserved → queued → running → succeeding|failing → succeeded|failed|skipped|cancelled|blocked

Fan-out 子索引：stepId#index 各自独立状态；父 step 聚合：succeeded_count / failed_count / total。

4.3 终端态不可逆（MUST）
done | failed | cancelled | blocked 为 run 终态；恢复仅通过新 run（可引用旧资产），禁止原地改终态为 running。

5. Intent 路由表
5.1 输入（MUST）

type TurnInput = {
  userText: string
  canvas: {
    projectId: string
    selectedNodeIds: string[]
    hasImage: boolean
    hasVideo: boolean
    referenceAssetIds: string[]
  }
  flags: {
    // 来自 catalog.enabled，非用户设置
    textToImage: boolean
    imageEdit: boolean
    textToVideo: boolean
    imageToVideo: boolean      // 当前常 false
    imageToMultiframe: boolean
    stitchFramesToVideo: boolean
  }
  quota: { creditsLeft: number; rateLimited: boolean }
  activeRunId?: string         // 同画布进行中 run
}
5.2 IntentResult（MUST）

{
  "version": "1",
  "goal": "video",
  "path": "image_to_multiframe",
  "capabilityHint": "image_to_multiframe",
  "tierDefault": "preview",
  "needSubjectCutout": false,
  "fanOutHint": null,
  "frameCount": 4,
  "durationSec": 5,
  "referenceAssetIds": ["asset_abc"],
  "targetNodeIds": ["node_sel_1"],
  "confidence": 0.86,
  "clarifyQuestion": null,
  "clarifySlots": []
}
5.3 路由表（MUST）
条件	path / capabilityHint	并行提示	未开通时
要静图 / 默认四方向	text_to_image；fanOutHint=4	四路可并行	—
有图要改图	image_edit	通常单路	—
无图要视频	text_to_video preview	单路	unavailable
有图 +「直接出视频/一键成片」且 allow_direct_i2v	image_to_video	单路	capability_unavailable，禁改生图
有图 + 普通做成视频（默认）	image_to_multiframe（再 stitch/I2V）	链内串行；帧生成层内可并行	对应 flag false → unavailable
用户点「生成正片」	同能力 tier=final	新 run；不与旧 preview 写同节点并行	—
含糊 / 多参考冲突	clarify	—	HITL 一句问清
只要聊天	无 capability	不调生成	—
配置 IMAGE_VIDEO_STRATEGY：MUST 默认 multiframe_first；allow_direct_i2v 为显式开关。

5.4 规则快筛 + LLM（SHOULD）
词表短接高置信路径。
含糊进低温度 Intent LLM。
IntentValidator：goal=video 不得落到仅 image 终态 path。
6. Todo DAG 完整 JSON Schema
6.1 顶层 Plan（MUST）

6.2 示例：四方向生图（并行）

{
  "planVersion": "1",
  "goal": "生成四个不同方向的预览图",
  "goalType": "image",
  "tier": "preview",
  "steps": [
    {
      "id": "s1",
      "title": "生成四方向预览图",
      "capabilityId": "text_to_image",
      "dependsOn": [],
      "parallelGroup": "g_directions",
      "fanOut": 4,
      "targetNodes": ["n_d0", "n_d1", "n_d2", "n_d3"],
      "tier": "preview",
      "argsIntent": {
        "promptBase": "赛博朋克街景",
        "directionVariants": ["正面", "侧面", "俯视", "逆光"]
      },
      "retryPolicy": { "maxAttempts": 2, "scope": "failed_index_only" },
      "onFailure": "keep_partial"
    }
  ],
  "runPolicy": {
    "partialFailure": "allow_partial_fanout",
    "maxConcurrency": 4,
    "maxReplan": 2,
    "autoStartPreview": true,
    "requireConfirmBeforeFinal": true,
    "reviewMode": "soft"
  }
}
6.3 示例：有图做成视频（串行主链）

{
  "planVersion": "1",
  "goal": "把参考图做成短视频样片",
  "goalType": "video",
  "tier": "preview",
  "sourceAssetIds": ["asset_ref_1"],
  "steps": [
    {
      "id": "s1",
      "title": "生成四方向预览图",
      "capabilityId": "text_to_image",
      "dependsOn": [],
      "parallelGroup": "g_directions",
      "fanOut": 4,
      "targetNodes": ["n1", "n2", "n3", "n4"],
      "tier": "preview",
      "onFailure": "keep_partial"
    },
    {
      "id": "s2",
      "title": "挑选最佳角度并抠图",
      "capabilityId": "image_edit",
      "dependsOn": ["s1"],
      "targetNodes": ["n_cut"],
      "tier": "preview",
      "inputFrom": [{ "stepId": "s1", "as": "reference", "index": 0 }],
      "onFailure": "fail_downstream"
    },
    {
      "id": "s3",
      "title": "多帧预览合成短视频",
      "capabilityId": "image_to_multiframe",
      "dependsOn": ["s2"],
      "targetNodes": ["n_video_preview"],
      "tier": "preview",
      "inputFrom": [{ "stepId": "s2", "as": "primary" }],
      "onFailure": "fail_run"
    }
  ],
  "runPolicy": {
    "partialFailure": "fail_downstream_only",
    "maxConcurrency": 4,
    "maxReplan": 2,
    "autoStartPreview": true,
    "requireConfirmBeforeFinal": true,
    "reviewMode": "off"
  }
}
6.4 依赖判定规则（写入 Planner system + Validator）（MUST）
有依赖：B 输入需要 A 产出；或写同一 targetNode；或产品链强制（样片→正片、多帧→合成）。
无依赖：只共用最初用户话/参考图，产物进不同预分配节点（四方向）。
拿不准 → 标依赖（宁可串行）。
7. PlanValidator 规则全集
校验失败 → plan_invalid；可重规划或 run failed。全部 MUST 除非标注 SHOULD。

ID	规则	失败码
V01	每个 step 含必填字段（§6）	plan_schema_invalid
V02	capabilityId 存在于目录	plan_unknown_capability
V03	catalog.enabled === true 且存在 binding（或显式 unbound 码）	capability_unavailable / capability_unbound
V04	dependsOn 引用存在的 id，无自依赖	plan_bad_dependency
V05	依赖图无环（Kahn / DFS）	plan_cycle
V06	goalType=video 的终端 step 输出类型必须为 video（或 multiframe→stitch 链完整）	plan_terminal_type_mismatch
V07	禁止 video goal 仅以 text_to_image/image_edit 结束	plan_video_degraded_to_image
V08	fanOut 若设：targetNodes.length === fanOut	plan_fanout_node_mismatch
V09	同层可并行的 steps：targetNodes 交集为空	plan_node_contention
V10	tier 与 run.tier 一致；final 不得写未确认的 preview 独占节点策略冲突	plan_tier_conflict
V11	argsIntent 规格在目录 tiers[tier] 允许范围内（或标记待 Gateway 裁剪）	spec_not_allowed
V12	inputFrom.stepId 在 dependsOn 闭包内	plan_input_not_dependent
V13	final run：MUST 带 sourceAssetIds 非空（样片→正片）	plan_missing_source_assets
V14	并行组 parallelGroup 内所有 step 依赖集合相同（SHOULD）	plan_parallel_group_inconsistent
V15	maxConcurrency ≤ 租户与 binding limits 的较小值（执行前再查）	plan_concurrency_too_high
V16	目标节点存在且可写（执行前再查一次）；计划时若已知已删 → 打回	asset_forbidden / node_missing
V17	未开通 image_to_video 时不得出现该 capability step	capability_unavailable
V18	MOCK adapter 仅非 prod；prod 计划含 mock → 拒绝	plan_mock_in_prod
重规划策略（MUST）：Validator 返回机器可读 violations[]；Planner 最多 maxReplan 次；仍失败 → run failed，Host 用人话说明，禁止删减视频步骤改生图交差。

8. Scheduler：拓扑、并行、fanOut、节点锁、预占、部分失败、重试、取消、恢复
8.1 拓扑分层（MUST）
根据 dependsOn 建 DAG，Kahn 分层：layers[0..L]。
仅当层内所有 step 的依赖均已 succeeded|skipped(按策略) 时，step 变 ready。
层内按 maxConcurrency 与预占结果并发；层屏障：进入下层前上层须达终态（成功或按 onFailure 跳过）。
8.2 并行与 fanOut（MUST）
fanOut=N：展开为 N 个子任务 stepId#0..N-1，各绑定 targetNodes[i]、变体参数。
同一 parallelGroup 共享并发预算。
四方向生图：默认 fanOut=4，partialFailure=allow_partial_fanout。
视频链：步骤间串行；若某步内部多帧，仅帧级可并行，合成步依赖全部所需帧。
8.3 节点锁 / lease（MUST 设计并实现最小版）
LockKey = projectId + nodeId
Lease: { runId, stepId, holder, expiresAt, mode: "write" }

执行前对 targetNodes 获取 write lease（TTL 建议 120s，可续租）。
冲突 → node_busy：同用户排队 SHOULD；跨用户/跨 run MUST 拒绝或按产品策略排队并提示。
步骤结束/取消释放；过期可被同 project 的 Scheduler 回收并标记旧任务 lease_lost → 失败可重试。
8.4 积分预占（MUST）
层或整 run 开始前：quote = sum(estimated)。
reserve(runId, layerId, amount, idempotencyKey) → 成功才 enqueue。
并行预占失败一半：MUST 整组不启动（先预占满 fanOut 再 enqueue）；返回 quota_exceeded 或 reserve_failed，Host 说明「额度不足，无法启动 4 路」。
配额只够部分 fanOut：MUST 二选一且对用户可见（配置 FANOUT_QUOTA_POLICY）：
reject_group（默认）
reduce_fanout_with_notice（降 N 并改 plan 展示与预占）
成功 settle；失败按 BILLING_PARTIAL refund/keep；取消后以 provider 终态为准回写 charged。
8.5 部分失败（MUST）
策略	适用	行为
allow_partial_fanout	四方向预览	成功 index 落画布；Host「2/4 成功」；失败 index 可重试
fail_downstream_only	视频链中间步	停下游；已成功上游保留
fail_run	关键合成步	run failed；触发退款策略
禁止：视频主链失败后改调 text_to_image 当作成功。

8.6 重试（MUST）
默认 failed_index_only；provider_timeout / rate_limited 可重试；content_blocked / capability_unavailable 不重试。
幂等键见 §15；重试不得新开「另一批」重复 fanOut 而不带原 key。
8.7 取消与改口（MUST）
cancel(runId, reason: user_cancel | user_revised | superseded | system)

run → canceling。
取消未完成子任务；Gateway/Adapter cancel(providerTaskId) 尽力而为。
UI：「已取消上一批」。
新 run MUST NOT 复用未完成 step 的进行中 provider 任务。
cancel_reason=user_revised 写入事件；流水保留已 charged，对账可查。
8.8 恢复 / 杀进程续跑（MUST）
客户端断线：MUST 可用 runId 查询状态与 parts 续看。
Scheduler 进程重启：从 DB 加载 running 层，对 queued/running step 向 Queue 对账；丢失 lease 则失败可恢复策略。
超时：binding.timeoutSec；超时 → provider_timeout，可按 retryPolicy 重试。
8.9 伪代码
function schedule(run):
  layers = topoLayers(run.plan.steps)
  for layer in layers:
    if run.status == canceling: break
    ready = [s for s in layer if depsOk(s)]
    expand = fanOutExpand(ready)
    if not reserveCredits(run, expand): fail(run, reserve_failed); return
    locks = acquireNodeLocks(expand)
    if locks.conflict: fail or enqueue_wait(node_busy); return
    awaitAll(executeWorker(expand), maxConcurrency)
    releaseLocks(locks)
    applyPartialFailurePolicy(layer)
    if layer.blocked: run.status = blocked; return
  maybeReview(run)
  if run.tier == preview: run.status = awaiting_user_final
  else: run.status = done

9. Worker + Tool Gateway + 能力目录对接
9.1 Worker（MUST）
单步职责：组装 args → Gateway.invoke → 轮询/订阅 job → 校验产物类型 → 写画布节点（CAS）→ emit step events → 可选触发 Reviewer。

9.2 Gateway 伪流程（MUST）
onInvoke(capabilityId, tier, args, ctx):
  cat = catalog.get(capabilityId)
  if !cat?.enabled: return Err(capability_unavailable)  // fail-closed
  bind = binding.get(capabilityId, env, tenant)
  if !bind: return Err(capability_unbound)
  if env==prod && bind.adapter startsWith "mock": return Err(plan_mock_in_prod)
  validate(args, cat.tiers[tier], cat.input)
  quote = pricing.estimate(...)
  // 预占已在 Scheduler；此处 verify reservationId
  job = queue.enqueue({
    adapter: bind.adapter,
    modelId: bind.modelId,
    bindingVersion: bind.bindingVersion,
    idempotencyKey: ctx.idempotencyKey,
    ...
  })
  return { jobId }

9.3 当前绑定事实（写作日）
capabilityId	绑定建议	enabled
text_to_image	百炼 qwen-image-2.0	true
image_edit	百炼 qwen-image-edit-plus	true
text_to_video	百炼 wan2.6-t2v	true（真实出片需验收）
image_to_video	待定	false
image_to_multiframe / stitch_frames_to_video	组合实现	按 flag
review_*	待定	可 soft off
Agent MUST 只认 capabilityId；换模型只改 binding。

9.4 Adapter 最小接口（MUST）

interface MediaAdapter {
  create(job: NormalizedJob): Promise<{ providerTaskId: string }>
  poll(providerTaskId: string): Promise<ProviderStatus>
  cancel(providerTaskId: string): Promise<void>
  mapError(err: unknown): HostErrorCode
}
NormalizedJob：prompt、assets、duration、size、tier；禁止把百炼字段漏到 Planner。

10. Reviewer：硬检查 / 内容审、soft/hard、挂载点
10.1 职责边界（MUST）
Reviewer 审产物，不审计划（计划归 PlanValidator）。
未启用时 reviewMode=off，过程中禁止展示「已质检通过」。
10.2 硬检查（MUST，可无 LLM）
检查	图	视频
文件可读 / MIME	✓	✓
非空、最小分辨率	✓	✓
时长 / 是否有有效帧	—	✓
输出类型匹配 capability	✓	✓（禁静图冒充）
失败 → review_hard_fail；按 soft/hard 策略。

10.3 内容审（SHOULD，能力 review_image / review_video）
符合提示词、主体一致、样片「真能动」。
视频可抽帧再审。
输出：{ score, pass, reasons[], labels[] }。
10.4 soft vs hard（MUST 可配）
mode	行为
off	不跑
soft	不阻断落画布；节点打标「质检未通过」；过程一行摘要；允许用户仍点正片（可再警告）
hard	不落违规/低质为「成功样片」；step 失败或 blocked；可重试该步
误杀申诉（SHOULD）：soft 下用户「仍要用」；hard 下「申请人工放行」记审计事件（P1 UI）。

10.5 挂载点（MUST）
preview 层成功后、进入 awaiting_user_final 前。
final 资产落位前。
MAY：每个 fanOut index 后（成本高，默认关）。
11. HITL / 样片→正片
11.1 确认点（MUST）
点	何时	UI
Clarify	Intent 含糊	Host 一句问清
计划/报价确认	autoStartPreview=false 或超积分阈值	ConfirmPart
样片→正片	preview 完成	画布样片 +「换个效果」「生成正片」
高危	删除节点级资产等	另案；可挂同一 interrupt 通道
11.2 样片→正片（MUST）
preview run → awaiting_user_final；画布节点 badge=preview。
「生成正片」→ 新 run：tier=final，sourceAssetIds 显式列出，能力同路径。
「换个效果」→ 取消未完成 + 新 preview run（改口流程 §13）。
正片与样片 分两次报价与扣费。
用户无档位设置；系统默认 preview。
11.3 awaiting_confirm vs awaiting_user_final
awaiting_confirm：执行前（计划/积分）。
awaiting_user_final：preview 已落画布后。
二者都可序列化恢复（对齐 RunState / interrupt）。
12. 画布契约
12.1 节点字段（MUST）

type CanvasNodeMedia = {
  nodeId: string
  type: "image" | "video" | "empty"
  assetId?: string
  assetVersion: number
  badge?: "preview" | "final" | "review_soft_fail"
  busyRunId?: string
  createdByRunId?: string
  createdByStepId?: string
}
12.2 落位规则（MUST）
样片/正片落画布；角标「样片预览」可播。
fanOut 预先占位空节点，避免挤叠。
写回 CAS：expectedAssetVersion；冲突 → node_version_conflict，不覆盖。
视频路径：不覆盖未确认的图片像素当「视频完成」。
12.3 选错 / 空画布 / 删节点（MUST）
场景	行为
空画布要图生视频	Intent clarify 或要求上传；不瞎规划
选错节点	计划写死 targetNodeIds；Host 可确认「将基于当前选中」
Todo 指向已删节点	执行前校验；node_missing；进程红态；可 HITL 重选
局部重绘 vs 整段视频	节点 busy / 能力互斥；冲突 node_busy
撤销进行中节点	MUST 撤销该节点即 cancel 对应 step，或禁止撤销 busy 节点
12.4 右侧 vs 画布（MUST）
右侧：进程 n/m 默认展开；「处理过程」默认折叠；短回复 + 按钮。
侧栏不大播放器、不刷 tool JSON。
最多极小缩略可点回画布节点。
13. 多人 / 多 run / 改口
13.1 同画布多 run（MUST）
同一 projectId 允许多个 run 仅当 targetNodes 无交集且锁不冲突。
冲突：返回 node_busy 或 run_superseded；策略配置 ON_CONFLICT=reject|queue。
每人/每会话可见自己的 Host 线程；画布节点锁跨会话生效。
13.2 改口（MUST）
用户新消息且 Intent 判定为修订进行中任务 → cancel(runId, user_revised)。
UI 提示已取消上一批。
开新 run；不继承 canceling 中的 step。
已落画布的成功部分：SHOULD 保留并标记「上一轮」；用户可删。
13.3 口头确认 vs 自动跑（MUST）
preview：autoStartPreview=true 时跳过计划确认，直接 reserving→running。
若用户在 awaiting_confirm 改计划：不得开工；Scheduler 仅在离开该态后启动。
14. 错误码与用户文案
code	含义	用户文案（建议）	可重试
capability_unavailable	未开通	该能力暂不可用	否
capability_unbound	无绑定	该能力暂不可用	否
spec_not_allowed	规格超限	请降低清晰度或时长	调参后可
quota_exceeded	欠费/配额	额度不足	充值后
reserve_failed	预占失败	额度不足，无法启动本批任务	是
rate_limited	限流	排队中，请稍后	是
content_blocked	合规	未通过安全审核	否
asset_forbidden	资产无权限	请重新上传参考	否
asset_expired	URL 过期	请重新上传参考	是
node_missing	节点已删	目标已删除，请重选	是
node_busy	节点锁冲突	该位置正在生成中	是
node_version_conflict	CAS 失败	内容已被更新，请刷新后重试	是
plan_invalid / 子码	计划非法	无法按当前目标规划，请换种说法	是
plan_video_degraded_to_image	企图视频变图	内部错误已拦截	否（打告警）
provider_timeout	上游超时	生成超时，可重试	是
provider_failed	上游失败	生成失败，可重试	是
idempotent_replay	重复提交	（静默返回原 job）	—
review_hard_fail	质检硬失败	未通过质量检查	可重试生成
cancel_timeout	取消未完成	已请求取消，上游仍可能短暂计费	—
run_superseded	被新 run 替代	已取消上一批	—
internal_error	未知	服务异常	是
MUST：禁止把上表统一成 internal_error 后静默改能力。

15. 幂等、计费、对账流水字段
15.1 幂等键（MUST）
idempotencyKey = hash(
  tenantId, projectId, runId, stepId,
  index?, capabilityId, tier, contentHash
)

fanOut：stepId + index。
重复点击 → idempotent_replay，返回原 job，不二次扣费。
15.2 计费状态机（MUST）
quoted → reserved → settled | refunded | partial_refund | cancelled_with_charge

样片/正片分两次。
失败退还策略配置化并写进流水。
取消后上游仍计费：本地 canceling，provider 终态回写 chargedCredits。
15.3 流水字段（MUST 每条媒体任务）
tenantId, projectId, sessionId, runId, stepId, index,
capabilityId, tier, bindingVersion, adapter, modelId,
providerTaskId, parallelGroup,
idempotencyKey, reservationId,
quoteCredits, chargedCredits, refundCredits,
errorCode, targetNodes, sourceAssetIds,
startedAt, endedAt, durationMs, cancelReason

16. 配额、限流、高峰降级、合规
16.1 配额 / 限流（MUST）
租户余额 + binding maxConcurrency + 全局 QPM。
打满 → quota_exceeded / rate_limited。
并行扇出受三者最小值约束。
16.2 高峰降级（MUST 显式）
允许（配置 + 对用户可见）

preview 分辨率在目录允许集合内降低。
final 排队过久 → 提示改等 / 取消；不自动改能力。
缓 final、保 preview。
禁止

video → image
multiframe → 单张静图冒充视频
prod → mock
换 capabilityId 不改计划展示
16.3 合规（MUST）
可选 moderation: required 挂在能力上；输入/输出拦截。
命中 → step/run blocked；进程 UI 红态；不落违规资产。
审计日志保留 request 元数据（按法务最小化）。
16.4 多参考 / 音视频输入（MUST 契约）
目录 maxRefImages/Videos；超限不可用而非瞎跑。
参考视频/音频未开通 → fail-closed。
多参考冲突 → clarify，Planner 不瞎融合。
17. 观测、SLO、告警
17.1 Trace（MUST）
Span：intent, plan, validate, reserve, step, gateway, provider, review, node_write。
属性含 §15.3 关键字段 + request_id。

17.2 SLO（建议初值，可调）
指标	目标
preview 图 P95 端到端	< 60s（依赖供应商）
preview 文生视频 P95	< 180s
计划校验失败率	< 5% 可归因于模型胡规划（重规划后）
静默降级企图	0（plan_video_degraded_to_image 告警）
重复扣费	0
取消后无主任务	尽力；cancel_timeout 可观测
17.3 告警（MUST）
plan_video_degraded_to_image / 任意 fail-closed 绕过企图
prod mock adapter 调用
预占与 settle 长期不一致
provider 错误率飙升
node_busy 异常升高（锁泄漏）
18. 安全
项	要求
凭据	仅 credentialRef；Worker/服务端解析；禁止进前端、prompt、日志明文
Prompt 注入	用户内容标注 untrusted；工具参数 JSON schema；禁止用户原文拼 shell
越权资产	提交前 ingest；校验 tenant/project 归属；过期重签；失败 asset_forbidden
权限	画布写操作校验 write token / 成员角色
供应面	Adapter 出站网络限制；SSR F 仅资产域白名单
19. 数据模型（表级字段）
19.1 agent_run
id, tenant_id, project_id, session_id, status, tier, goal, plan_json, intent_json, source_asset_ids, cancel_reason, replan_count, review_mode, created_at, updated_at, terminal_at

19.2 agent_step
id, run_id, step_key, index, capability_id, status, depends_on_json, target_nodes_json, parallel_group, reservation_id, job_id, provider_task_id, error_code, attempt, created_at, updated_at

19.3 agent_event
id, run_id, step_id, type, payload_json, created_at
类型含：status_changed, plan_updated, progress, user_facing_text, cancel_requested, review_result…

19.4 credit_ledger
id, reservation_id, run_id, step_id, idempotency_key, quote, charged, refund, state, created_at

19.5 node_lease
project_id, node_id, run_id, step_id, expires_at, mode

20. API / 事件（对前端的 part 类型）
20.1 Host Parts（MUST）
part type	用途	右侧表现
plan / todo_plan	进程 n/m	默认展开
thinking	思考	进「处理过程」折叠
tool_status	工具进度	折叠内；禁 JSON 刷屏
confirm	HITL	确认卡
text	Host 话术	正文
error	结构化错误	文案 + code
run_status	run 态变更	驱动按钮区
artifact_ref	缩略/跳转节点	小图，非大播放器
20.2 建议 HTTP/WS
POST /api/host/turn — 用户消息
POST /api/host/runs/{id}/cancel
POST /api/host/runs/{id}/confirm-final
GET /api/host/runs/{id} — 续跑续看
GET /api/capabilities — enabled + 允许规格（无 Key）
WS/SSE：parts 流式推送
20.3 ConfirmFinal body 示例

{
  "previewRunId": "run_123",
  "sourceAssetIds": ["asset_v_preview"],
  "sourceNodeIds": ["n_video_preview"]
}
21. 灰度与迁移
21.1 从当前 Host 路径迁移（MUST 步骤）
契约先行：落地 Plan schema + Validator（可先只串行单步 plan，直连生图也发单步 plan，修 Todo 空白）。
Gateway 接目录：三能力 enabled 绑定；I2V disabled fail-closed。
Scheduler v1：串行 + 四方向 fanOut 并行组。
UI：进程外露 + 过程折叠 + 样片角标（oil-ui 交接已有）。
计费幂等字段对齐流水。
feature flag：MULTI_AGENT_SCHEDULER=1 按租户灰度；关闭时走旧路径但仍禁静默降级。
final run 与改口取消。
Reviewer soft 挂预览后。
打开节点锁与多 run 冲突策略。
全量后删旧「无 plan 直连」旁路。
21.2 回滚
Flag 关回旧 Host；保留目录门禁（门禁不回滚）。
bindingVersion 可回滚上一版本。
22. 测试与验收矩阵
ID	场景	期望
T01	四方向生图	进程显示并行 2/4…；四节点落画布；部分失败可 2/4
T02	无图文生视频	串行/单路；失败不落单图冒充
T03	有图要视频 + I2V off	capability_unavailable 或走 multiframe；无静默单图成功
T04	goal=video 计划被改成只生图	Validator 打回；告警
T05	重复点击	不双扣费；idempotent_replay
T06	预占不足 4 路	整组不启动或降级并告知
T07	用户改口	旧 run canceling；新 run；UI 提示
T08	删节点后执行	node_missing；红态
T09	样片→正片	新 run + sourceAssetIds；两次流水
T10	合规拦截	blocked；不落违规资产
T11	杀进程	GET run 可续看
T12	取消后 provider 仍成功	流水 charged 可对账；不写覆盖用户已删节点（锁/归属）
T13	右侧 UI	无能力/档位设置；过程折叠；样片在画布
T14	Reviewer soft	落画布+打标；hard 阻断
T15	双 run 抢同节点	node_busy
T16	prod MOCK	拒绝
23. 上线检查清单
 Plan JSON Schema + PlanValidator 全规则（含 V07 视频降级）上线
 Catalog 三绑定真实可用；I2V disabled 行为验收
 Scheduler：串行正确 + 四方向并行 + 节点 lease
 积分：quote/reserve/settle/refund 与幂等键
 取消 / 改口 / canceling 态与流水
 run 查询续看
 错误码表与前端文案
 静默降级企图监控 = 0
 UI：进程 / 折叠过程 / 样片角标 / 无设置项
 Confirm final 新 run
 合规 blocked 红态
 Trace 含 bindingVersion + providerTaskId
 Feature flag 与回滚演练
 T01–T16 回归通过
 凭据不进仓、不进前端
24. 附录
24.1 与即梦 / 可灵交互对齐
点	即梦/可灵习惯	我们
预览位置	画布/中心节点	样片落画布，角标「样片预览」
侧栏	对话 / 历史，不当唯一播放器	进程 + 折叠过程 + 确认；不大播放器
多 Agent	用户无感	Host-only；内部 DAG 并行
24.2 与 OpenAI / Claude / 阿里概念对照（短）
我们	OpenAI	Claude	阿里（Spring / 百炼）
Host 保留作答	agents-as-tools	父 Agent + Subagent 摘要	Supervisor / 应用单入口
Planner Todo	结构化输出 Agent	plan 权限 / 子 Agent	Graph / Sequential + planning
HITL	interruptions / RunState	canUseTool / permissions	Human-in-the-loop / Interrupt
并行 fanOut	编排层并行工具	并行 subagent	ParallelAgent
门禁	tool guardrails	permissions deny	应用鉴权 + 自建 Gateway
共识：生产重点是编排边界、工具门禁、HITL、观测计费——不是页面上开多个模型对话框。

25. 一句话
可上线多 Agent = Host 对外 + Intent 自动路由 + Planner 出可校验 DAG + Validator fail-closed + Scheduler 按依赖并行（四方向并行、视频串行）+ Gateway/目录对账 + 样片在画布、正片确认 + 改口/锁/配额/合规/续跑全有契约；禁止静默换菜。