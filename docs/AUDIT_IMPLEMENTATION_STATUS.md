# aiVideo 实现盘点

日期：2026-10-15。范围：当前工作区源码与本机方案；只新增本报告，未改业务代码、未 push、未删文件。

## 1. 仓库与分支快照

路径缩写：
- **M** = `/Users/weilengdezaoshang/Documents/项目/aiVideo`。
- **O** = `/Users/weilengdezaoshang/.codex/backups/aivideo-consolidation-20261015-084435/retired-8445`。
- 下表路径均相对对应根目录；`路径::符号` 表示证据定位。

两仓均执行了 `git status -sb`、`git log --oneline -15`、`git branch -vv`。

主干 M：`main`，HEAD `6e87892`；工作区大量 tracked 修改及 untracked 新模块，本文“已落地”指当前源码存在，不表示已提交或已部署。最近提交前三条：`6e87892` 画布打开自动适配、`247d1d3` 连线跟随与分层、`970b7ff` 时间线音频与导出契约；15 条范围截至 `d790b8a`。分支另有 agent-flow-evaluation、canvas-perf-evidence、grok-canvas-host、publish-pages、sync-cloud-platform、sync-cloud-platform-release；后两条分别显示相对 origin/main ahead 3/behind 2 与持平，publish-pages behind 2。

旧线 O：`codex/grok-canvas-host`，HEAD `970b7ff`；同样存在大量未提交/未跟踪实现。最近前三条 `970b7ff`、`d6ade43`、`95247e6`；15 条范围截至 `a2e5b78`。与 M 属于同一 Git 仓库的不同 worktree，共享分支列表。原 `/Users/weilengdezaoshang/Documents/项目/aiVideo-grok-canvas-host` 已不存在，O 仍可审计。归档原因与日期见 M `docs/runbooks/8443-consolidation.md` 的“本机验收与归档”。

默认启动：M `scripts/dev_stack.py::API_PORT` 为 8443，README“日常开发”明确 8443、不再启动 8445/8798；O 同名脚本默认也为 8443，而其 README 的上下文实现段仍称“8445 Host 面板”。因此旧线名称不能直接等同其当前脚本端口。未启动服务、未核验当前监听端口。

方案核对：已读取 Downloads 中 TECH_AGENT_CONTEXT_REQUEST（v3）、HANDOFF_AGENT_CONTEXT_REQUEST、HANDOFF_MERGE_8445_INTO_8443、HANDOFF_CAPABILITY_CATALOG、HANDOFF_INTENT_VIDEO_P0，以及 M 的 TECH_INTENT_VIDEO_ROUTING、TECH_MULTI_AGENT_PRODUCTION。指定目录递归检索未找到 TECH_CAPABILITY_CATALOG.md、HANDOFF_BAILIAN_VIDEO_PROVIDER.md、HANDOFF_PROCESS_UI_ALIGN.md；对应严格要求不能宣称全部对齐。

## 2. 总览

**已落地 10 / 部分 6 / 未做 1 / 已废弃 0，共 17 项。** 状态以 8443 为准；旧线运行入口已退役，不等于其中全部源码删除。

## 3. 逐项现状

| ID | 状态 | 8443 证据（M） | 8445 证据（O） | 缺口一句话 |
|---|---|---|---|---|
| A1 | 已落地 | `apps/web/shared/agent-protocol.ts::AgentRequestSchema` 包含全部指定字段；`apps/api/backend/agent/media/request_context.py::AgentRequestContext` 同构校验 | `apps/web/shared/agent-protocol.ts::AgentRequestSchema`；`apps/server/src/domain/agent-context.ts` 重用该 schema | 主干是 TS Zod + Python 同构，非 Python 直接共享 Zod；测试：`tests/backend/test_host_request_context.py::test_client_identity_history_and_duplicate_ids_are_rejected` |
| A2 | 已落地 | `apps/web/canvas/agent/store.ts::send` 在 await 前调用 `captureRequestContext`；`agent/request-context.ts::buildRequestContext` 仅组装轻量上下文 | `tests/agent-request-contract.test.ts` 有禁止全文/历史字段断言；`tests/agent-request-transport.test.ts` 覆盖传输 | selection/viewport 同步冻结；doc_version 使用 ensureSaved 返回版本，并非严格同一瞬间；测试：`tests/canvas-request-context.test.ts`“请求冻结选区与视口…” |
| A3 | 部分 | `request_context.py::resolve_context` 按持久化 objects 解析并忽略客户端描述；`service.py::MediaHost.create` 保存 targets_source/selection_ids/mention_ids | `apps/server/src/domain/agent-context.ts::resolveAgentContext` 组装分来源 targets；`api/agent-runs.ts::installAgentRuns` 注入 recent_ops_self | 主干媒体入口未接完整 L0/history/self-op Context Builder；媒体目录搜 recent_ops 仅见拒绝客户端字段，不能视为已实现后端 self-op |
| A4 | 已落地 | `request_context.py::resolve_context` 对改图强制有效 image；`tests/backend/test_host_request_context.py::test_c01_edit_resolves_saved_image_and_redacts_body`、`test_c10_video_metadata_cannot_turn_image_edit_into_video_generation` | `tests/agent-request-contract.test.ts` 的 C01/C04/C10 与遗留 video 元数据回归 | 已覆盖所列 C01/C10；不代表任意自然语言均准确，见风险 |
| A5 | 未做 | `apps/web/canvas/agent-panel.tsx::AgentPanel` 使用 textarea；`agent/request-context.ts::buildRequestContext` 未填 mentions；协议支持不等于 UI | `docs/IMPLEMENT_AGENT_CONTEXT.md`、`domain/agent-context.ts::resolveAgentContext` 有协议/来源支持 | 两仓画布输入入口搜索无 @ 下拉/chip 实现；MVP 尚未做；无自动化，仅手工未知（指 @ UI） |
| A6 | 已落地 | `request_context.py::resolve_context`、`service.py::MediaHost.create` 校验 revision 并报 STALE_VERSION；`canvas.py::write_asset` 节点 assetVersion+租约 CAS | `apps/server/src/adapters/postgres/agent-context.ts::PostgresAgentContext.admit/beforeEvent`；`tests/agent-request-durable.test.ts` CAS 测试 | 采用持久化 revision/snapshot_seq；异步落位使用节点 CAS，并非要求整个 Doc 在生成期间零变化；测试：`test_unsupported_or_stale_context_fails_closed` |
| B1 | 已落地 | `media/contracts.py::Capability/Binding/MediaStep`、`catalog.py::Catalog`、`planner.py::plan_intent`；`media/api.py::GET /api/host/capabilities` | `apps/server/src/capabilities/catalog.ts::CapabilityDirectory`、`gateway.ts::CapabilityGateway` | Planner 使用 capabilityId+tier，部署绑定决定模型；测试：`test_production_mock_and_missing_reviewer_fail_closed`、`test_browser_cannot_override_binding_or_skip_revision` |
| B2 | 部分 | `planner.py::validate_plan` 拒绝 plan_video_degraded_to_image；`plan_intent` 有图默认关键帧→frames_to_video，未开通由 Catalog 拒绝 | `apps/server/src/tools/video.ts::createUnavailableVideoTool`；`apps/server/tests/production-video.test.ts`，覆盖拒绝图片内容 | 已识别为 video 的计划有硬门禁，但意图词表不一致仍可把视频请求识别成 image；测试：`test_unavailable_video_and_unsupported_specs_never_become_images` |
| B3 | 部分 | `apps/api/backend/providers/cloud.py::CloudProvider.video_request/generate_video` 为 DashScope 异步视频协议；`media/contracts.py::Binding` 要求 endpoint+credentialRef；`catalog.py::Catalog` 拒绝生产 mock | `apps/server/src/tools/video.ts::createProductionVideoTool`；`docs/BAILIAN_MODEL_CONFIGURATION.md` | 具备 Cloud 视频适配与目录关闭机制，但指定百炼 handoff 缺失；未证明地域/业务空间专门配置及 videoToolEnabled、MOCK_VIDEO 字面开关对齐；测试 `tests/backend/test_cloud_video.py`，本次未跑 |
| B4 | 已落地 | `planner.py::plan_intent(tier="preview")` 自动分流；`service.py::MediaHost.final`；`agent-panel.tsx::AgentPanel` 正片确认按钮，Host 模式无能力/档位选择器 | `domain/media-routing.ts::automaticIntent`；`apps/server/tests/automatic-preview.test.ts` 自动预览与正片重放 | 自动样片→用户确认正片已实现；集成测试 `test_four_direction_durable_jobs_credit_reservation_canvas_and_final`，本次未跑 |
| C1 | 部分 | `agent/activity-log.tsx::AgentActivityLog/ToolRow/AgentThinking`、`agent-evidence.tsx::AgentEvidence`，工具展开、流光、证据与回画布存在 | `apps/web/canvas/host-part-view.tsx`；`tests/canvas-host-parts.test.tsx` 折叠与流式完成保持测试 | 思考不可展开；AgentActivityLog 调用 AgentThinking 未传 text，最新思考预览未接线；SSE 快照更新不能等同逐 token 流式 |
| C2 | 已落地 | `apps/web/canvas/app.tsx::AgentPanel` 仍为挂载入口；主干全仓搜 CanvasCreationPanel 无实现 | `apps/web/canvas/canvas-creation-panel.tsx::CanvasCreationPanel` 为旧线实现 | 主干未被旧面板替换；测试 `tests/canvas-agent-panel.test.tsx`（本次未跑） |
| C3 | 部分 | `activity-log.tsx::useExpansion` 运行自动展开、结束 300ms 收起、手动优先；`AgentThinking` 固定不可展开 | `tests/canvas-host-parts.test.tsx`“过程默认折叠，手动展开在流式完成后保持…” | 已改活动流，非仅旧版；但“展开思考/流式三态”未齐，缺原 HANDOFF_PROCESS_UI_ALIGN 无法完整比对；测试 `tests/canvas-agent-activity.test.tsx` |
| D1 | 已落地 | `docs/runbooks/8443-consolidation.md` 明确模块取舍；`media/service.py::MediaHost`、`canvas.py::write_asset`、共享 AgentRequest 已在主干 | `capabilities/gateway.ts::CapabilityGateway`、`api/agent-runs.ts::installAgentRuns` 是原实现 | 有移植记录，采用主干等价实现，非整仓 merge；当前新增实现多未提交，未见对应 Git merge 提交 |
| D2 | 部分 | `README.md`、`scripts/dev_stack.py::API_PORT` 已统一 8443；主干 CanvasCreationPanel 搜无 | O 保留完整旧面板/Host 和迁移；归档位置由 M 统一记录说明 | 默认启动已清理，但 M `package.json::dev:creation-server` 与 apps/server 仍保留；不能宣称重复 Host 代码全部删除；无自动化，仅手工未知（指彻底清理） |
| E1 | 已落地 | `media/canvas.py::allocate/write_asset`、`credits.py::reserve/settle`、`service.py::confirm/final` | `apps/server/src/agent/canvas-result.ts`、`adapters/postgres/agent-context.ts::beforeTurnEvent` | 主干已接节点、积分与确认；测试 `test_four_direction_durable_jobs_credit_reservation_canvas_and_final`、`test_host_four_direction_real_delivery_and_duplicate_receipt`，本次未跑 |
| E2 | 已落地 | `media/service.py::MediaHost.tick/cancel/retry` DAG 调度；`media/api.py::events` 支持 after/Last-Event-ID seq；`media/reviewer.py::review_one` | `apps/server/src/agent/host.ts`；PostgresAgentContext 持久化运行与取消 | 有编排/取消/恢复及 SSE，不要求多个独立 LLM 才算多 Agent；测试 `test_recreated_host_recovers_and_node_lease_prevents_competing_writer`、`test_partial_failure_retry_is_per_index_idempotent_and_does_not_repeat_successful_jobs`，本次未跑 |

## 4. 风险

1. **视频可能变生图（静态可达，未实跑端到端）**：M `request_context.py::resolve_context` 将英文 `animate` 视为视频转换，但 `planner.py::plan_intent` 的 is_video 词表只有 video/animation 等，未含 animate。例如选中有效图片发送 `animate this image`，resolve 留下图片参考，Planner 可选择 image_edit；validate_plan 因 goalType 已是 image 而不会拒绝。这是意图识别漏口，门禁本身不能补救。
2. **发送状态与版本不完全原子**：M `agent/store.ts::send` 在 await ensureSaved 前冻结 selection/viewport，之后才填 doc_version/client_ts。保存等待期间内容可改变，后端按新版本读取旧选区 ID；CAS 能防版本不匹配，不能证明发送瞬间语义一致。已有冻结测试覆盖选区变化，尚未证明该时间窗口的内容一致性。
3. **兼容入口绕过新快照约束**：M `media/contracts.py::HostTurn.context` 可选；`service.py::MediaHost.create` 仅有 context 时 resolve。旧客户端仍可直接提供 selectedObjectIds/referenceAssetIds；权限和资产检查存在，但不能获得新协议的来源分离保证。

对“选中图却因遗留 taskType 被覆盖成无关视频”，本次未发现主干新协议路径存在该覆盖，C01/C10 测试通过；上述意图与兼容路径仍需保留边界测试。未调用真实 Provider，不对实际出片作结论。

## 5. 建议下一批（最多 3 项，依赖顺序）

1. 统一 resolve 与 Planner 的结构化意图，并补保存期间版本一致性/兼容入口约束；验收：animate/video/短片与改图否定句矩阵、C01/C10、保存竞态全部不串能力，视频请求缺视频步骤必拒绝。
2. 在主干 AgentRun 内补完整 Context Builder，再接 @ 下拉/chip；验收：mentions/selection 来源可追溯、客户端描述不入模型、他人 ops 不进 self、请求体仍无历史与节点全文。
3. 补齐活动流思考展开与真实增量接线，并核准百炼部署参数；验收：折叠/展开/流式前端测试及 Provider 未配置关闭、地域/业务空间请求契约测试通过，再以独立测试设施回归媒体 DAG/积分/取消。

## 6. 附录：关键文件与验证边界

M 关键路径：
- `apps/web/shared/agent-protocol.ts`；`apps/web/canvas/agent/request-context.ts`；`apps/web/canvas/agent/store.ts`。
- `apps/api/backend/agent/media/{request_context,contracts,planner,catalog,service,canvas,credits,api,reviewer}.py`。
- `apps/web/canvas/{app,agent-panel,agent-evidence}.tsx`；`apps/web/canvas/agent/activity-log.tsx`。
- `apps/api/backend/providers/cloud.py`；`deploy/host-catalog.example.json`；`docs/runbooks/8443-consolidation.md`。

O 关键路径：
- `apps/server/src/domain/{agent-context,media-routing}.ts`；`apps/server/src/api/agent-runs.ts`。
- `apps/server/src/adapters/postgres/agent-context.ts`；`apps/server/src/capabilities/{catalog,gateway}.ts`。
- `apps/web/canvas/{canvas-creation-panel,host-part-view}.tsx`；`docs/IMPLEMENT_AGENT_CONTEXT.md`；`docs/BAILIAN_MODEL_CONFIGURATION.md`。

本次实跑：
- `.venv/bin/python -m pytest -q tests/backend/test_host_request_context.py tests/backend/test_host_media_contracts.py`：15 passed，1 个依赖弃用警告。
- `node --import tsx --test tests/canvas-request-context.test.ts tests/canvas-agent-activity.test.tsx tests/canvas-agent-activity-model.test.ts`：18 passed。

未运行全量 npm run verify、独立数据库/Redis 集成、旧线测试、服务启动与真实百炼出片；报告中的其他测试名仅证明自动化用例存在。统一记录内历史全量通过数字不作为本次实跑结论。
