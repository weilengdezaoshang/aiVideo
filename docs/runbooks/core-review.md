# Core 计划审核（块 10）

`backend.core.review.validate(plan, run_context)` 是审核员的唯一入口：纯函数、无副作用、不调模型、不改单。本块实现第 ① 道（单子合法）、第 ② 道（类型链接得上）与第 ③ 道中的 invariants 规则核对；第 ③ 道的小模型对照由块 11 通过 `intent_extensions` 扩展点接入。不建数据库表、不注册 HTTP 路由、不接事件处理函数。

## 输入契约

`plan` 可以是原始 JSON（模型输出，或用户在确认卡上手改后回传的计划）或已解析的 `Plan`，两者走同一入口。步骤 id 必须是 `tN`，上游输出引用写作 `$tN.output`，资产引用写 `{"asset_id": ..., "version_id": ...}`。

`run_context` 由 Host 从冻结的 Run 行（scope / invariants / version_lock）加上用户积分余额、Run 预算和画布快照组装：

| 字段 | 内容 |
| --- | --- |
| `user_id` / `team_user_ids` | 资产归属判定：本人或团队成员 |
| `user_credits` / `run_budget` | 预估花费的两条上限 |
| `scope.tools` / `scope.writable_regions` | 工具（`id` 或 `id@version`）与写入区域白名单 |
| `version_lock.tools` | 冻结的工具定义：参数 schema、输入类型、输出类型、`ambiguous_without`、单价 |
| `invariants.deliverable` | 数量、模态、比例或尺寸、时长、必须引用的资产 |
| `canvas.assets` | 画布快照中的资产及归属、尺寸、时长 |
| `replan.previous` / `replan.blocked_task_ids` | 上一版计划与 blocked_by_model 的任务 id |

`run_context` 是 Host 提供的可信数据：格式非法直接抛错，不当作计划退回。`plan` 格式非法则退回 `invalid_plan`，错误信息带字段路径。

## 三道检查与退回码

`ReviewReason = {gate: legal|types|intent, code, step, message}`，`message` 是模型可读的中文一句话并指向具体步骤。所有检查一次跑完再汇总，退回时可以看到全部原因。

**第 ① 道 legal（单子合法）**

| code | 规则 |
| --- | --- |
| `tool_not_locked` / `tool_disabled` / `tool_version_mismatch` | 工具必须存在于锁定版本、未被停用、版本一致 |
| `tool_out_of_scope` / `region_out_of_scope` | 工具与写入区域必须在 scope 白名单内 |
| `param_unknown` / `param_type_invalid` / `param_enum_invalid` / `param_below_minimum` / `param_above_maximum` / `param_required_missing` | 参数符合 schema 与能力范围；必填参数缺失一律退回，没有豁免；上限示例：`t2: image_to_video 的时长 30 秒超出上限 10 秒` |
| `asset_unknown` / `asset_not_owned` | 引用资产（含 ask_user 步骤的引用）必须在画布快照中且属于用户或其团队 |
| `run_budget_exceeded` / `credits_exceeded` | 预估花费（步骤 `estimated_cost` 或工具单价求和）不超过 Run 预算与积分余额 |
| `video_params_uncertain` | 视频工具的 `ambiguous_without` 参数（如时长）缺失且上游没有 ask_user 步骤时，追加该退回指引；计划参数是静态的，澄清结果只能通过下一版计划回填，所以缺必填参数的视频单即使带了 ask_user 也不放行 |
| `video_degradation` | 重规划时同 id 任务不得从视频改成图片 |
| `blocked_model_switch` | blocked_by_model 的任务不得换工具（模型）重做 |
| `duplicate_task` | 同一计划内任务指纹（工具 + 版本 + 参数 + 输入）不得重复 |
| `duplicate_step_id` / `ask_user_with_tool` / `missing_tool` | 结构性硬错误 |

`scope.readable_assets` 的授权判定留给服务层：本块只按快照归属拒绝他人资产，避免双重白名单造成假退回。`scope.tools` 与 `scope.writable_regions` 都按白名单语义执行：留空即不允许任何工具/区域（未声明 region 的步骤不做区域检查）。指纹去重的范围是当前提交的计划；跨计划版本的重试与重派由调度层依据执行状态判断。重规划降级与换模型拦截按上一版计划同 id 匹配，且用当前 version_lock 解析上一版工具——上一版工具若已不在锁中，该步对比被跳过（同 Run 内锁是冻结的，实际风险低）。

**第 ② 道 types（类型链接得上）**

| code | 规则 |
| --- | --- |
| `missing_upstream` / `missing_dependency` | `$tN.output` 与 depends_on 必须指向存在的步骤（ask_user 步骤的引用同样检查） |
| `undeclared_input` | 引用必须落在工具声明的输入参数上 |
| `input_type_mismatch` | 上游输出类型（或资产 kind）必须属于下游参数允许的输入类型；视频交给抠图在此退回 |
| `dependency_cycle` | depends_on 与输出引用合并成图后不得有环 |

**第 ③ 道 intent（invariants 规则核对）**

最终交付物 = 输出图片/视频、且没有任何下游（既无依赖它、也无引用其输出）的步骤；仅有 depends_on 排序依赖也会把上游排除出交付物。按交付链路对照 `invariants.deliverable`，两侧的数字与比例先归一化再比对（`18:32` 与 `9:16` 视为相同，`2.0` 视为 `2`）：

| code | 规则 |
| --- | --- |
| `deliverable_count` | 数量（每步取 `count` 参数，缺省 1）必须等于要求 |
| `modality_mismatch` | 模态（图片/视频）必须一致 |
| `aspect_mismatch` / `aspect_unknown` | 比例沿参数→上游任务→资产尺寸推导后必须一致；推不出来就退回 |
| `size_mismatch` | 未给比例但给了宽高时按精确尺寸核对 |
| `duration_mismatch` / `duration_unknown` | 视频时长必须等于要求，推不出来退回 |
| `required_asset_missing` | 必须引用的资产要出现在交付物的传递闭包里 |

典型用例：用户要 3 张竖版、计划产出 2 张横版 → 同时退回 `deliverable_count` 与 `aspect_mismatch`。

## 第 ③ 道扩展点

`validate(plan, run_context, intent_extensions=[rule])`：`rule(view) -> list[ReviewReason]`，`view` 是只读的 `PlanView`（计划、上下文、每步解析出的 ToolSpec、交付物 id）。块 11 的小模型对照实现为注入的 rule，返回的 reason 与规则原因同构（用 `intent_reason()` 构造），审核主流程不变。扩展 rule 抛错会向上传播，视同宿主代码缺陷。

## 验证

```sh
.venv/bin/python -m pytest -q tests/backend/test_core_review.py
npm run verify
```

`tests/backend/test_core_review.py` 为纯逻辑测试（无数据库、无网络）：每条规则至少一个通过与一个退回用例，必含五个场景——视频交给抠图、时长超限、3 竖版对比 2 横版、依赖成环、引用他人资产；另含 20 步计划单次审核耗时 < 20ms 的性能断言（实测约 1ms 量级）。计划格式、重复 id、确认卡手改计划同入口、扩展点注入、invariant 比例归一化、整值 float 数字、空 scope 白名单语义、ask_user 输入校验、depends_only 排除交付物均有覆盖。

## 边界与后续

- 审核结果 `ReviewResult` 只有 `pass` / `reject` 两种；「需澄清」由宿主根据 `video_params_uncertain` 等原因自行转成 ask_user 流程，本块不替宿主做澄清决策。
- `estimated_cost` 随结果返回，供确认卡展示报价；预占、扣费、提交供应商仍属后续执行块。
- 同节点多写者冲突、并行扇出限制属调度器（PlanValidator/Scheduler）职责，不在本块。
- 本块不落库、不发事件；把 validate 接进计划写入与执行准入流程（复用 0014 审核表或核心域 Plan 版本）由后续块完成。
