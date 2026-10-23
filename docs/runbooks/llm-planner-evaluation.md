# LLM Planner 评测

96 条人工标注在 `tests/fixtures/llm-planner-cases.jsonl`。分类是 `single`、`ordered_multi`、`reference`、`ambiguous`、`chat`、`unsupported`、`video_missing`、`injection`，每类 12 条。这是标注，不是模型准确率。

执行已经接上 `EvaluationRunner`、`POST /api/evals/v1/runs`、`scripts/eval.py runs create` 和评测后台新建页。`plannerMode=rule` 跑 `RulePlanner`。`plannerMode=llm` 只使用部署 route 上的 `LLMPlanner`。没有 route 或没有 apiKey 时每条是 undetermined，不回退 Rule，也不把 Rule 结果写成 LLM。注入的协议替身必须带 `eval_binding`（`kind=synthetic`、`synthetic=true`、`audit.source`），结果标 synthetic。注入对象自称 `kind=llm` 不会被当成部署默认构造。

本轮没有使用付费凭据，也没有真实 LLM 基线。最终集成后运行离线门禁得到以下 Rule 基线；目标准确率尚未设置。

| 指标 | Rule 实测 |
| --- | --- |
| 工具选择 | 28/96（29.17%） |
| 顺序 | 0/14（0%） |
| invariants | 41/96（42.71%） |
| 首次审核通过 | 96/96（100%） |
| P50 / P95 | 28 / 51 ms |
| 每次 token | 0（规则，无模型调用） |

首次审核通过包含合法澄清表单，不能视为用户目标完成率。以上延迟是本机离线 Rule 观测，不是 LLM 延迟；LLM 的准确率、token 与延迟均未测量。提示词版本 `supervisor-v1`，数据集 SHA256 `b89736068609ca44644350129bbb0c1632ce58fc52129ca6d22d0552a3234a20`。

## 数据

每行一个 JSON 对象，由 `backend.evaluation.planner_cases.PlannerCase` 校验。

| 字段 | 含义 |
| --- | --- |
| `case_id` | 稳定 ID |
| `category` | 上面八类之一 |
| `language` | `zh` 或 `en` |
| `pair_id` | 注入用例与对照用例的配对键；无配对时为 `null` |
| `context` | 冻结输入 |
| `expected` | 标注，不是模型观察 |

`context.request` 含 `message` 与 `selected_assets`。`context.snapshot.objects[]` 含 `object_id`、`asset_id`、`type`、`text`、`label`。注入句子可以出现在 `text`、`summary` 或 `upload_note`，只作为数据。`context.scope.readable_assets` 列出本轮可读 UUID。`context.scope.tools` 是能力名。`context.invariants.deliverable` 始终为 `null`。

`rule_context` 把 `upload_note` 放进 `snapshot.uploads`，并带上 `language`。`context.recent_outputs[]` 的字段是 `output_id`、`asset_id`、`modality`、`label`、`rank`。`rank` 为 1 表示最新。

工具名按任务 `t1`、`t2`… 的顺序写在 `expected.tool_names`。允许 `text_to_image_preview`、`image_edit_preview`、`text_to_video_preview`、`image_to_video_preview`、`detect_subject`、`cutout`、`read_canvas`、`read_asset`、`ask_user_form`、`answer_only`。`set_invariants` 不是工具选择。`expected.dependencies` 形如 `{"t2": ["t1"]}`。蒙版绑定只用于抠图：`detect_subject` 紧挨 `cutout`，且 `tN.mask` 等于 `$t(N-1).output`。

`expected.invariants.deliverable` 只记录用户说出的字段：`count`、`modality`、`aspect_ratio`、`width`、`height`、`duration_seconds`、`required_asset_ids`。视频必须有明确秒数才写入视频工具和 `duration_seconds`。`video_missing` 只问 `duration_seconds`。

注入用例与对照用例共享 `pair_id`，两边的 `context.request` 和 `expected` 必须相同。恶意句子只出现在不可信字段。

`tests/backend/test_llm_planner_cases.py` 只做数据治理，不构造模型输出，也不代替实测基线。

## 评分

`backend.evaluation.planner_evaluation.score` 只比较标注和一次已经产生的计划。它不调用规划器，也不用 `expected` 填写观察值。工具名按步骤顺序严格相等。顺序看 `depends_on` 是否与标注一致并且依赖步骤排在前面。没有依赖标注的用例不进入顺序分母。审核回执没有 `pass`/`reject` 时，首次审核为 `null` 并保留原因。

工具目录证据使用 `catalog.lock.content_hash`，不是只对工具名字做哈希。Run manifest 的 `promptTemplateHash` 来自 `core.supervisor.prompts.load_prompt()` 的模板正文，`promptTemplateVersion` 是 `supervisor-v1`。`promptAssemblerSourceHash` 单独记录 `context.assemble_messages` 源码。

## 怎么跑

Planner 数据集只在 mock 执行模式创建。LLM 质量走 `plannerMode=llm` 和部署 route，不走 live 画质 Provider，也不填画质裁判。预算可以写在 run 上。客户端 URL、`apiKey`、`credentialRef` 会被 `create_run` 拒绝。`plannerRouteId` 必须是部署 route id，服务端对照 `wiring.deployment_routes` 校验，不能只靠页面。

评测后台：`npm run dev:evaluation` 后打开新建评测。Planner 数据集显示「Rule 基线」和「LLM 部署路由」。选 LLM 时再选 route id。route 列表来自 `GET /api/evals/v1/planner/routes`，响应只有 `id`、`label`、`provider`、`model`。没有配置时列表为空，页面写明未配置，提交按钮保持禁用。路由文件非法时接口返回 422「部署路由配置非法，已拒绝列出」，不回显路径或密钥。媒体数据集和 Agent 流程源题库的创建表单不变。

列出路由：

```bash
curl -sS http://127.0.0.1:8788/api/evals/v1/planner/routes
```

创建（store 换成隔离目录；数据集需已冻结为 `llm-planner`）：

```bash
python3 scripts/eval.py runs create \
  --dataset llm-planner \
  --run-id planner-rule-1 \
  --planner-mode rule \
  --store /tmp/aivideo-planner-eval
```

```bash
python3 scripts/eval.py runs create \
  --dataset llm-planner \
  --run-id planner-llm-1 \
  --planner-mode llm \
  --planner-route <部署 route id> \
  --store /tmp/aivideo-planner-eval-llm
```

`--planner-mode` 和 `--planner-route` 属于 `scripts/eval.py runs create`。`python -m backend.evaluation.planner_cli` 只跑 Rule 基线，没有 `--planner-route`。

真实 LLM 还要同时具备：

- `AIVERO_PRODUCTION_AGENT_ROUTES_FILE` 指向合法部署 route 文件
- `AIVERO_CREDENTIALS_FILE` 里有该 route 的 `credentialRef` → `apiKey`

缺一则 factory 保持 undetermined，`llmTested` 不为 true。本轮文档和测试都没有读取或调用付费凭据。

等价的进程内入口是 `EvaluationRunner.create_run(..., mode="mock", sandbox_config={"plannerMode": "rule"})` 或 `{"plannerMode": "llm", "plannerRouteId": "<id>"}`，然后 `execute_run`。摘要在该 run 目录的 `planner-summary.json`。

## 离线门禁

提示词文件、`context.py` 或 `planners.py` 变更时，pre-commit 与 CI 跑：

```bash
PYTHONPATH=apps/api .venv/bin/python -m backend.evaluation.planner_gate
```

每次使用新的独占目录。已有内容的 `--output` 拒绝覆盖。stdout 打印 `gateEvidence` 与 `ruleSummary`。门禁只拦协议、注入控制流和数据损坏。Rule 匹配率没有准确率门槛。离线 scripted 模型只做控制流，不代表测过真实 LLM。离线门禁会清掉 route 与凭据环境变量。

## 指标

最终 baseline 在 runner、API、CLI 和后台入口集成完成后重跑。数字由 Codex 填入，当前留空。

| 指标 | Rule 基线 | 真实 LLM |
| --- | --- | --- |
| 覆盖 | 96/96 | 未测 |
| 工具选择 | 28/96（29.17%） | 未测 |
| 顺序 | 0/14（0%） | 未测 |
| invariants | 41/96（42.71%） | 未测 |
| 首次审核 | 96/96（含合法澄清） | 未测 |
| tokens | 0（规则） | 未测 |
| 延迟 | P50 28 / P95 51 ms | 未测 |

2026-10-22 用当时的 `planner_cli` 得到的 Rule 工具选择 32/96（约 33.3%）是过期中间版本，不能当作当前基线，也不代表 LLM。

## 模板版本

当前模板版本是 `supervisor-v1`，正文在 `apps/api/backend/core/supervisor/prompts/supervisor-v1.txt`。哈希以运行时 `load_prompt().content_hash` 为准，改正文后再跑一次 Rule 基线才会写入新的 `promptTemplateHash`。不要在文档里手填未重跑的哈希。
