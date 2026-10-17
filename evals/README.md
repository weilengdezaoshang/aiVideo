# 生成质量评测体系

对[技术方案 v0.1](./技术方案-图生图文生视频与时间线-v0.1.md) §15 评测实验的落地:固定参数批量生成 → VLM 判分 → 对比报告。当前覆盖**文生图**,文生视频与图生视频桶待真实后端验收后扩充。

## 组成

| 文件 | 职责 |
| --- | --- |
| `golden-set.jsonl` | 中文评测集,每行一条:id、bucket(桶)、prompt、固定 params(seed!)、questions(原子判分问题 + expect 接受答案 + weight) |
| `../scripts/eval.py` | 三个子命令:`run`(批量生成)→ `judge`(判分)→ `report`(摘要/对比) |

## 用法(.venv/bin/python)

```bash
# ① 管线自测(完全离线,mock 生成 + stub 判分)
python3 scripts/eval.py run   --provider mock --run-id demo
python3 scripts/eval.py judge data/evals/demo --judge stub
python3 scripts/eval.py report data/evals/demo                # 单次摘要
python3 scripts/eval.py report data/evals/<新> data/evals/<旧>  # 换模型前后对比

# ② 真实评测(cloud 生成 + Qwen-VL 判分;产生真实费用,仅换厂商/换模型/大版本升级时跑)
python3 scripts/eval.py run   --provider cloud --run-id qwen-a
python3 scripts/eval.py judge data/evals/qwen-a --judge qwen-vl
```

qwen-vl 判分器默认 `https://dashscope.aliyuncs.com/compatible-mode/v1` + `qwen-vl-max-latest`,可用 `EVAL_JUDGE_BASE_URL / EVAL_JUDGE_API_KEY / EVAL_JUDGE_MODEL` 覆盖;Key 缺省回退 `SWARMUI_IMAGE_API_KEY`。协议形状以实测为准。

## 边界与纪律

1. **mock 运行的分数不代表模型质量**,报告会强制标注;它的用途是验证管线与可复现性(同 seed 产物逐字节一致,有测试锁定)。
2. 评测产生真实调用费用:**只在换厂商、换模型、模型大版本升级时跑**;费用随每次调用进入 traces,不与额度混淆。
3. 扩集规则:全部中文、来自真实使用措辞(不写"基准考题腔");每个桶 ≥15 条才算可对比;改集必须换 run 对比,不允许用旧分数混排。
4. 管线测试(`tests/backend/test_eval_pipeline.py`)已纳入 `npm run verify`,评测代码自身的回归是零成本的。

## 创作 Agent 流程（L0–L5）

流程评测走同一套 `scripts/eval.py`、冻结数据集、类型化规则和门禁。它真实启动 `apps/server` 的 Memory composition root（Node 22 + tsx），写出 Trace v1，再按规则判分。不建第二套平台，不调用付费模型，不把 cloud/VLM 画质分写进流程分。

数据集在 `evals/datasets/agent-flow-l*.jsonl`。源文件用顶层 `steps` / `expect` / `mocks`，校验时收进 `input.params`。`mocks.imageProvider` 只能是 `stub`。

| 层 | 文件 | 条数 | 在查什么 |
| --- | --- | --- | --- |
| L0 | `agent-flow-l0-protocol.jsonl` | 4 | SSE 起止、会话/轮次 ID、Trace v1、错误协议 |
| L1 | `agent-flow-l1-routing.jsonl` | 3 | 短 prompt 直出；复杂 prompt handoff；批准后往返 |
| L2 | `agent-flow-l2-plan.jsonl` | 3 | 唯一计划、状态推进、完成步骤不回退 |
| L3 | `agent-flow-l3-interrupt.jsonl` | 4 | 批准、取消、重复确认、相反确认 |
| L4 | `agent-flow-l4-continue.jsonl` | 4 | 取消或生成中停止后继续；新需求清除草稿 |
| L5 | `agent-flow-l5-queue.jsonl` | 8 | 真实 429、并发、幂等、取消、超时、背压、过期、驱动拒绝 |

### 命令

需要 Node 22。本机可执行文件：

```bash
export AIVIDEO_NODE=/Users/weilengdezaoshang/.nvm/versions/node/v22.23.2/bin/node
```

单层（把 `L0` 换成 `L1`–`L5`，或 `protocol` / `routing` / `plan` / `interrupt` / `continue` / `queue`）：

```bash
python3 scripts/eval.py flow --suite L0 --node "$AIVIDEO_NODE"
```

整包 26 条：

```bash
python3 scripts/eval.py flow --suite agent_flow --node "$AIVIDEO_NODE"
```

退出码：0 全通过且门禁 pass；1 有失败；2 未知 suite 或用例非法。未知 suite 会列出可选名称。文生图命令（上面的 `run` / `judge` / `report` 和 `runs`）不变。

### 指标

流程通过率是计划内规则通过的 trial 数 / N_plan。检查项 ID 就是失败原因码。它不是画质分。报告里的「生成成功率」对流程 run 会是 0，因为 stub 图片不写入评测对象库；看「Agent 流程」一节。

### 失败排查

失败行格式：

```text
layer=L4 caseId=l4-stop-continue reason=continue_min_completed trace=/path/to/traces/<runId>.json
```

- `reason` 是检查项 ID。协议：`sse_start`、`sse_monotonic`、`sse_terminal`、`conversation_id`、`turn_id`、`trace_v1`、`error_protocol`、`http_status`。路由：`route`、`must_ask_user`、`no_handoff`、`tool_*`。计划：`plan_unique`、`plan_advanced`、`plan_completed`、`plan_step_count`、`plan_ids_stable`、`completed_kept`。确认：`success_artifacts`、`cancel_new_artifacts`、`cancel_new_credits`、`approve_min_artifacts`、`approve_min_credits`、`duplicate_idempotent`、`confirm_conflict`。继续：`continue_has_plan`、`continue_cursor_kept`、`continue_completed_kept`、`continue_not_redone`、`continue_min_completed`、`draft_cleared`。队列：`rate_limit_429`、`retry_after`、`concurrency_cap`、`queue_idempotent`、`queue_cancel`、`queue_timeout`、`queue_backpressure`、`queue_expiry`、`bullmq_refused`。
- Trace 在 `{store}/runs/{runId}/trials/{trialId}/agent/traces/{服务端runId}.json`。适配器崩溃时没有 Trace，原因是 `server_error`，stderr 在 trial 错误里。
- 负样本不进黄金集。复杂广告若被标成 `route=direct`，执行会完成，质量失败，reason 为 `route`，Trace v1 里仍有 `handoff_to_planner`。`tests/backend/test_agent_flow_eval.py` 锁定这一点。

### 2026-10-10 实测

Node v22.23.2，stub 生图，store 在 `/tmp/aivideo-flow-eval-*`。

| 命令 | 结果 |
| --- | --- |
| `flow --suite L0 --run-id af-l0` | 4/4，门禁 pass |
| `flow --suite L1 --run-id af-l1` | 3/3，门禁 pass |
| `flow --suite L2 --run-id af-l2` | 3/3，门禁 pass |
| `flow --suite L3 --run-id af-l3` | 4/4，门禁 pass |
| `flow --suite L4 --run-id af-l4` | 4/4，门禁 pass |
| `flow --suite L5 --run-id af-l5` | 8/8，门禁 pass |
| `flow --suite agent_flow --run-id af-all` | 26/26，门禁 pass |
| `flow --suite not-a-suite` | 退出码 2 |

整包报告：`/tmp/aivideo-flow-eval-all/runs/af-all/reports/1/report.md`。流程通过率 1.0。失败定位为空。

### 未支持

Redis / BullMQ 没有实现。L5 的 `bullmq_refused` 只验证非 memory 驱动被拒绝，错误原文是「BullMQ/Redis 为显式未实现的扩展骨架；本地请使用 memory 异步队列，不能静默回退」。这不能记成 Redis 通过。没有 L6/L7、画布落位和反思集。流程分不代表画质。

### 如何证明评测有效：故障注入验收

一键运行 `npm run eval:agent:faults`。需 Node 22、根 npm 依赖与 Python 开发环境；可用
`AIVIDEO_NODE=/absolute/path/to/node22 npm run eval:agent:faults` 指定 Node。
默认新建独立临时目录，不改数据集、生产源码或已有 run。每个故障针对相应层的目标 case，
依次执行正常 PASS → 注入 FAIL → 关闭注入 PASS；任一断言失败，命令以非零码退出。
不是把期望改成失败，也不是伪造 artifact：替身通过真实 Host/ports 执行，仍使用现有 runner/grader。

分别复现：

```sh
python3 scripts/verify_agent_eval_sensitivity.py --fault f1
python3 scripts/verify_agent_eval_sensitivity.py --fault f2
python3 scripts/verify_agent_eval_sensitivity.py --fault f3
```

三条命令均应输出 PASS / FAIL / PASS，并最终输出验收 Pass。
`--store /absolute/new-directory` 可指定新的证据目录；已存在的 run 不会被覆盖。
输出 `sensitivity.json` 中红单包含 `suite`、`caseId`、`reason`、`tracePath`，
另保留原 grader 的 `checkId`。普通评测报告继续显示原规则码，映射如下。

| 故障 | suite / caseId | 注入方式（仅评测） | reason / 原规则码 | 注入前 / 注入 / 恢复 | 验收 |
|---|---|---|---|---|---|
| F1 | L1 / l1-complex-handoff | LLM 替身遗漏 handoff | missing_handoff / route | PASS / FAIL / PASS | Pass |
| F2 | L3 / l3-cancel | 错误将拒绝送为批准，真实 enqueue 出图 | artifact_after_cancel / cancel_new_artifacts | PASS / FAIL / PASS | Pass |
| F3 | L4 / l4-cancel-continue | 发送继续前丢失 ContinuationDraft | no_plan_after_continue / continue_has_plan | PASS / FAIL / PASS | Pass |

2026-10-10 实测：三项均定位到正确层和 case，Trace v1 文件实际存在。
证据目录 `/tmp/aivideo-sensitivity-final/sensitivity.json`（本机临时文件，不纳入 Git）。
自动回归位于 `tests/backend/test_agent_eval_sensitivity.py`。

`AGENT_EVAL_FAULT=f1|f2|f3` 只由 `apps/server/src/eval/run-flow-case.ts` 的测试入口读取，
不在 `app/main` 或生产业务层启用。验收脚本显式清除继承的故障值，测完恢复环境；
每次独立子进程与 Memory ports，无需修改源码或重启服务。
本次只覆盖流程正确性，不涉及付费模型、画质、L6/L7 或真实 Redis 能力。

### 新版评测工作台

`npm run build:web` 后执行 `python3 scripts/evaluation_web.py`，访问
`http://127.0.0.1:8788/`。这是显式隔离的本地评测服务，复用现有评测 API、runner、
scheduler 和报告，证据落在 `data/eval-web/evaluation`，不使用生产数据库或付费凭据。
总览可选整包或 L0～L5 单层，启动后留在本次评测工作区，详情由用户主动打开；数据集、画质评测、审核、比较与成本入口保留。
故障卡当前提供终端验收命令，不把尚未执行的验收显示成已通过。
本次实测 L1 3/3、L4 4/4、agent_flow 26/26。页面状态与计数来自真实 API。

### 人工审核：完成后自动送审

CLI 与队列运行都由 runner 完成钩子统一写入审核任务，创建完成后才展示 run 完成。任务绑定 trial、素材、规则版本及策略哈希；重复完成/重试不重复创建。同一 trial 命中多个原因时按必送→风险→随机优先，自动分不会被人工审核覆盖。

默认只审核效果/主观题或不可靠的自动判定：

- **必送**：judge error（包含超时、无法解析）、自动判定无法确定、用例 `needsHumanReview: true`、分数位于参考阈值 ±ε 模糊带。默认参考阈值 0.8、ε=0.05。显式配置 `gateScoreThreshold` 时，质量门禁与自动判定冲突也必送；门禁读取相同的质量分门槛。未配置分数门槛时，门禁继续沿用必需检查项，不凭分数臆测冲突。
- **风险**：默认开启；画质分数落在 `grayMin=0.4` 至 `grayMax=0.7`（含端点），或同一检查项多裁判不一致。
- **随机**：仅效果题自动 PASS，默认 5%，可关闭或调比例，使用固定 seed 可复现。
- **不送**：普通画质 FAIL 不自动等同必须重判；Agent 流程规则 PASS/FAIL 默认不送，Trace+reason 保留自动判定。可独立开启 `reviewAgentFlowFails` 做失败归因抽检，不改写规则分；Agent 用例显式标记 `needsHumanReview` 才属于额外必送。
- **模拟管线**：mock 运行、mock Provider 或 stub 裁判的效果分默认排除，不代表真实质量。`includeSynthetic=true` 仅用于显式测试自动送审；`enabled=false` 关闭整个 run 的自动审核（包括标记题与 Agent 归因）。

新建评测可配置以上开关和阈值；策略执行前冻结到 `manifest.reviewSampling`。未传 run 配置时使用 `EVAL_REVIEW_SAMPLING` JSON 配置，否则使用默认值。例如：

```json
{"enabled":true,"required":"fail_or_undetermined","riskRate":1,"randomRate":0.05,"grayMin":0.4,"grayMax":0.7,"riskThreshold":0.8,"riskMargin":0.05,"gateScoreThreshold":null,"reviewAgentFlowFails":false,"includeSynthetic":false,"seed":0}
```

`required` 保留旧协议取值以兼容已有调用：默认 `fail_or_undetermined` 在 v2 表示上述必送条件，普通 FAIL 不再必送；`none` 关闭必送，`all` 显式审核所有符合类型与非模拟过滤的效果题。旧 `agentFailures=true` 仍视为显式开启失败归因。策略版本哈希包含所有阈值与开关。

完成提示自动显示送审数量与「处理自动审核任务」入口，不强制跳页。人工审核页每 4 秒刷新，可按 run 筛选；未命中显示「本 run 未命中送审策略」，无需粘贴三个 ID。任务中文原因旁保留原码：`judge_error / judge_unreliable / score_ambiguous / score_gate_conflict / flagged_case / score_gray / judge_disagreement / random / agent_flow_failure`。手动送审仅针对需人判断的题；纯程序规则（含 Agent 流程）不展示常规送审按钮，API 也拒绝常规手动重判。纯规则证据不会因灰区或随机抽样送审。显式 `needsHumanReview` 和开启的流程失败归因抽检仍保留；归因抽检不改写规则结果。人工必审未完成会阻止发布门禁自动通过。

本地验收：`npm run dev:evaluation`；`PYTHONPATH=apps/api:tests/backend .venv/bin/python -m pytest tests/backend/test_auto_review_sampling.py -q`。测试使用隔离目录和模拟裁判，不调用付费服务。


### 成本与压缩：配对硬校验，不出全未知报告

一次合法的成本对比必须**同时**满足（`backend/evaluation/pairing.py`）：

1. 同一 `datasetId`，且用例冻结快照（case set hash）一致；快照不一致禁止总费用对比；
2. 一方 `compression=off`（基线）、一方 `compression=on`（对比）；`manifest.compression.role="baseline"` 是显式基线标签；
3. 两次 run 均已成功完成（status=completed）；
4. 两侧均已结算费用（预算账本存在 settled 记录）且存在服务商上报 token（provider_reported usage）。

缺任一条：`POST /api/evals/v1/validate-pair` 与 `POST /api/evals/v1/compression-report` 返回 4xx，
body 为 `{"error": 中文总结, "reasons": [{"code", "message"}]}`。原码：`same_run / dataset_mismatch /
snapshot_missing / snapshot_mismatch / run_not_success / not_compression_pair / missing_cost / missing_tokens`。
mock/回放运行天然缺费用与 token，原因文案会说明「需要带预算记账的 live 运行」——这是数据事实，
不是可绕过的警告；宁可少报一次，也不出误导性的全未知报告。

入口与交互：

- **运行详情（主入口）**：成功的画质 run 显示一键配对——压缩跑找未压缩基线、未压缩跑找压缩运行
  （`GET /api/evals/v1/suggest-pair?run_id=...`）；找不到时展示中文原因与「一键新建配对运行」，
  预填同数据集 + 反向压缩开关 + live 模式，live 源运行会继承预算单价与裁判模型，并锁定数据集版本
  保证快照一致。Agent 流程 run 不参与成本对比。
- **成本与压缩页**：默认展示系统推荐的一对（不传 run_id 时取最新可配对运行）；生成前先跑
  validate-pair，未通过不调用报告生成，展示「无法对比」卡。高级手动选择里基线列表只显示与本次
  运行同数据集的成功运行，并标注每条 run 的压缩开关。
- **结果页**：一句话结论（费用/Token 百分比变化 + 通过率 + 门禁 + 推广结论）、费用/Token/通过率/
  门禁数字卡（涨跌着色；若某卡无数据则明确「本 run 未记账，不计入成功对比」）、异常与退化列表。
  部分用例未对齐时按对齐集合标注「样本不全：对齐 X / 计划 Y」；validate 响应带
  `coverage.alignedCases / alignedCompletedCases / baselinePlan / compressedPlan`。
- **批次比较页**保持旧 `/comparisons` 语义（快照不一致时降级为公共用例交集并显式说明），
  不套用压缩配对规则——那是模型/平台 A/B 对比的入口。

新建评测表单明示压缩开关（提交后 run 详情显示 `compression: on/off`）；mock 运行也如实记录该开关，
配对校验会精确报 `not_compression_pair / missing_cost` 而不是误报快照问题。费用来自 run 预算账本
（live 经预算网关逐调用结算），token 只认 provider_reported usage；生成响应暂不携带 usage，
token 主要来自 VLM 裁判调用。历史无 cost 的 run 在「无法对比」卡中解释原因，不做数字臆测。

本地验收：`PYTHONPATH=apps/api:tests/backend .venv/bin/python -m pytest tests/backend/test_pairing_validation.py -q`
（隔离目录，费用经账本注入、token 经 attempts usage 注入）；前端交互见
`node --import tsx --test --test-force-exit tests/evaluation-pairing.test.tsx`。


### 阶段验收记录（2026-10-10）

本次实际执行，未调用付费模型。界面读磁盘六份 JSONL：L0～L5 分别 4/3/3/4/4/8 题，共 26 题。
L1 只显示 3 条路由题，L4 只显示 4 条续跑题；说明卡包含「测什么 / 用户步骤 / 怎样算通过 / mock」，原始 JSON 默认收起。

```sh
npm run eval:agent:faults -- --store "$PWD/data/eval-acceptance/2026-10-10-closure-v2"
PYTHONPATH=apps/api:tests/backend .venv/bin/python -m pytest tests/backend/test_agent_eval_sensitivity.py tests/backend/test_agent_flow_eval.py -q
npm run verify
```

再次复现请换一个**新的** `--store` 目录；已有运行会被拒绝覆盖。单项可追加 `--fault f1`、`--fault f2` 或 `--fault f3`。
脚本在正常阶段清除 `AGENT_EVAL_FAULT`，仅故障阶段设置对应值；默认应用不启用注入，开关只在 eval 入口读取。

| 步骤 | 层 / caseId | 实际结果 | reason（原始失败检查项） |
| --- | --- | --- | --- |
| 1. 无注入整包 | L0～L5，26 题 | PASS 26/26 | 无 |
| 2. F1 路由 | L1 / l1-complex-handoff | FAIL | missing_handoff（route：期望 handoff_planner，实测 direct） |
| 3. F2 取消仍出图 | L3 / l3-cancel | FAIL | artifact_after_cancel（cancel_new_artifacts：期望 0，实测 3） |
| 4. F3 续跑丢计划 | L4 / l4-cancel-continue | FAIL | no_plan_after_continue（continue_has_plan：期望 true，实测 false） |
| 5. 关闭注入整包 | L0～L5，26 题 | PASS 26/26 | 无 |

三个目标 case 另各执行注入前 PASS → 故障 FAIL → 恢复 PASS。语义 reason 为验收映射，原 grader 检查码和原始 expected/observed 不改写。
执行真实 Memory Host 与 stub ports，Trace/SSE 观测交给现有 `typed-rules v1`（source=rules）判定，即本项目的流程 trace-rule 路径，不用预设结果替代执行。
协议拒绝及部分队列探针没有进入 Agent 回合，`tracePath=null`，记录实际 `observationPath`；F1/F2/F3 的 Trace v1 均存在。

证据（本机隔离运行产物，不提交 Git）：
- `data/eval-acceptance/2026-10-10-closure-v2/acceptance.json`：五步结果，整包每题的 caseId、reason、tracePath、observationPath。
- 同目录 `sensitivity.json`、`console.log`：每个故障的三阶段记录；`runs/f1-fault`、`f2-fault`、`f3-fault` 下保留原始 grades、Trace 与 observation。
- `data/eval-acceptance/2026-10-10-closure/ui-l1/page.png`、`ui-l4/page.png`：筛选后的题量和默认说明卡截图。
- `data/eval-acceptance/2026-10-10-closure-v2/t2i-smoke/acceptance.json`：原文生图 runner 的独立 mock 冒烟 PASS，实际产出 1 个素材；不代表画质。

验收清单：A 看题三项通过；B 五步通过；默认无注入；t2i 入口与 mock 跑测保留。本阶段只补验收脚本和记录，不扩展人工审核、成本对比或 L6。

最终验证：`npm run verify` 通过（前端 393 项；后端 520 项，2 项跳过）。验证日志保存在上述证据目录的 `verify.log`。

## Reflection（L7）验收

L7 增量接入现有 `agent_flow` runner 和规则 grader，新增四题：好夹具一次接受；坏夹具一次后变好；持续坏到两次重试上限并输出 issues；关闭审核保持旧版行为。整包现在包含 L0～L5、L7 七份 JSONL，共 30 题，以上 26 题记录是加入 L7 前的历史验收。

`reviewOutcome` 检查最终结果，`reviewAttempts` 检查重试次数，`reviewRun` 检查 Trace 中审核证据，`escalationText` 检查用户说明实际包含审核 issues。失败码包括 `review_outcome`、`unexpected_retry`、`review_not_run` 和 `missing_escalation`。Trace 的 `reviewHistory` 保存各轮证据，`evidence.review.attempt` 从 1 开始，`reviewAttempts` 从 0 开始。

F4 仅在离线 Node harness 注入：审核故障适配器忽略低分及关键项失败，将坏图直接接受；L7 应检出没有发生应有的重试。生产入口不读取 `AGENT_EVAL_FAULT`。运行 `python3 scripts/verify_agent_eval_sensitivity.py --fault f4 --store <新目录>`，应得到 PASS / FAIL / PASS。

2026-10-10 实测（无付费调用）：

- 创作服务 36 项测试通过，含三种 finalize 决策、硬检查、审核关闭、关键项缺失、免费重试、重试重新积分确认与取消后继续，以及生成重试途中停止后换执行 ID 仍保留上限。
- 整包 30/30 通过，L0～L5 与 L7 均绿色。证据：`/tmp/aivideo-reflection-acceptance-20261010/acceptance.json`。
- 最终 F4 实现验收 PASS / FAIL / PASS，失败码 `unexpected_retry`，证据：`/tmp/aivideo-reflection-f4-final-20261010/sensitivity.json`。
- 工作台题库、启动与现有工作台回归 31 项通过，支持 L7，新增审核字段均有中文说明。

完整仓库 `npm run verify` 的状态在下方记录；临时证据不提交到 Git。硬检查默认检查 mock 元数据，旧工具仅提供 URL 时只能检查产物状态/URL；真实适配器必须补实测元数据。默认 stub 证明流程闭环，不证明图片语义质量。

Reflection 单阶段时的校验记录（下文 Reviewer 阶段已重新跑全仓并通过）：

- `npm run typecheck`、`npm run build:web`、`npm run lint` 通过；本次修改的 Python 文件 Ruff 通过。
- 全量 `pytest -q`：531 passed、2 skipped；随后新增 L7 API 预览/启动测试单独 1 passed。
- `npm run verify` 未全绿：最新前端全量 394/397，三个失败均在范围外新加入的 `tests/evaluation-pairing.test.tsx`（成本报告/配对详情）；相关工作台回归另跑 31/31。
- 单独 `npm run verify:backend` 在 Ruff 阶段被范围外 `tests/backend/test_pairing_validation.py:15` 未使用的 `Config` 导入拦住；因此另跑全量 pytest 取得上述真实结果。
- 日志：`/tmp/aivideo-reflection-verify.log`、`/tmp/aivideo-reflection-pytest.log`、`/tmp/aivideo-reflection-server-test.log`、`/tmp/aivideo-reflection-l7-api.log`。未覆盖成本配对实现或移除其测试来制造绿色。

## 可插拔 Reviewer 验收

默认工厂使用 `StubReviewerProvider`，`evidence.review.reviewer="stub"`；显式 real 使用独立 HTTP `RealReviewer`，模型名进入同一证据。新增 typed `ReviewInput` 与 `ReviewResult`，Host 传入作品、创作需求、任务类型、rubric 标识和 Trace 上下文。真实审核目前只支持 image，默认关键项为相关性、明显残缺、比例。

环境变量：`REVIEWER_MODE`（默认 stub，非法值回退）、`REAL_REVIEWER_API`（完整 chat/completions URL）、`REAL_REVIEWER_MODEL`、`REAL_REVIEWER_API_KEY`（仅 env）、`REAL_REVIEWER_TIMEOUT_MS`（默认 15000，上限 15000）、`REVIEW_RUBRIC`（image/video JSON 数组映射）；原阈值及重试配置沿用。`createApplication` 允许 `reviewer: {mode, real: {api, model, timeoutMs}}`，已有 port 注入优先。

超时/空响应/解析失败策略统一为 fail → retry，适配器每次只请求一次。失败 issues 中文化，HTTP 状态可读，不回显远端错误体和异常详情；超限仍由 Reflection 输出说明，积分确认逻辑沿用。AbortSignal 用户取消不会被转成质量失败。

可选冒烟：`REAL_REVIEWER_SMOKE=1 node --import tsx --test apps/server/tests/real-reviewer.smoke.test.ts`。需要配置 API、模型、Key，使用仓库茶饮 JPEG 检查真实 Host 的 accepted 和真实模型 reviewer；默认或无 key 时 skip，CI 不依赖真实服务，也不声称 skip 代表真实画质通过。此轮未执行付费调用。

2026-10-10 最新 stub 回归：L0～L5、L7 共 30/30；F1～F4 全部 PASS / FAIL / PASS，整包恢复后再次 30/30。证据：`/tmp/aivideo-reviewer-acceptance-20261010/acceptance.json` 与 `sensitivity.json`。Reviewer HTTP mock 覆盖成功、超时（含响应读取挂起）、坏 JSON、空响应、评分非法、关键项缺失、HTTP 拒绝、无 Key、用户取消、本地文件、real 工厂 Trace，以及失败重试不绕过积分确认。

本轮最终校验：`npm run verify` 通过（前端 397/397，后端 532 passed、2 skipped）；创作服务额外执行 50 passed、1 skipped（真实冒烟默认跳过）。Typecheck、构建、ESLint、Ruff 全部通过，日志为 `/tmp/aivideo-reviewer-verify.log` 与 `/tmp/aivideo-reviewer-server.log`。此前 Reflection 阶段的配对检查阻塞已不再出现在本轮校验中；本轮未改动成本配对实现。
