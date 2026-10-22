# 块 3：统一工具定义与能力版本

本块只提供定义、版本发布/读取、参数与类型校验规则、价格估算、错误和识别结果的纯封装。没有新增生成调用、扣积分、画布写入、候选自动选择或完整审核准入。模型导出是任务单描述 schema；不能将这些名称直接注册为模型可执行的写工具。

## 一份定义，多处导出

`backend.core.tools.build_catalog(legacy_catalog, version, settings)` 读取现有 `agent.media.Catalog` 的固定 binding、档位、价格与尺寸，读取现有 `READ_TOOLS` 的参数 schema 和超时；不调用 `prepare`、上传或服务商接口。一个生成工具名对应一个固定能力/档位，例如 `text_to_image_preview`；模型不能指定或替换收费模型。

- `catalog.to_model_tools(user, version_lock)`：Chat Completions function schemas，只包含已绑定、已定价、启用且灰度分组匹配的工具。用户身份、积分余额不影响导出；没有会员字段。`user.rollout_group` 必须由服务端可信配置提供。
- `catalog.to_validator_rules(version)`：同一份参数/结果 schema、类型端口、范围、价格与灰度规则。
- `catalog.to_worker_config(name, version_lock)`：同一份版本的 adapter、固定 binding、价格、失败映射和执行元数据。返回配置，不执行。
- `estimate_cost(resolved_tool, params)`：先校验参数，再算固定价或绑定档位积分 × count；未知价格或不可用工具报错，不默认为免费。
- `get_tool(tx, name, version)` / `load_catalog(tx, version, expected_lock=...)`：按数据库已发布版本读取，没有 latest 回退。

`tool_catalog_versions` 保存完整快照、schema_version、SHA-256、发布者与时间；`tool_definitions` 按 `(catalog_version, name)` 索引定义和哈希。发布在调用方事务内完成；同版本并发发布只有一个成功，另一个收到 `CatalogVersionConflict`。数据库触发器拒绝 UPDATE、DELETE、TRUNCATE；定义索引只能插入与封存快照相同的内容。加载会检查快照、索引与哈希一致性，缺失或损坏时拒绝使用。它不修改其他块的状态机或 outbox。

Run 创建方将 `catalog.lock.model_dump()` 放入已有 `Run.version_lock.tool_catalog`，未来恢复方先按该版本加载并核验哈希。发布新版本不会修改旧版本或已有 Run。返回的数据均为独立副本，不能通过修改嵌套字典污染内存快照。

## 实际能力与价格边界

生成积分来自已部署 Host Catalog 的 `tiers[preview|final].credits`，不把 example 中的数字当生产价格。尺寸、时长、fps 为绑定档位的固定值，数量由 `AIVERO_TOOLS_MAX_COUNT` 进一步限制。图片编辑和图生视频保留单参考图约束；图片序列最少两个不同关键帧（实际图像差异仍由现有工人检查）。

局部重绘复用 staged 蒙版路径：ComfyUI，或实际绑定为 SiliconFlow Fill 的模型；会隐式替换收费模型的绑定不导出。没有独立扩图、独立超分辨率放大的执行适配器，因此清单保留其不可用定义，模型列表不包含它们。视频拼接复用现有 FFmpeg 时间线导出，但没有现成 Agent 积分费率，必须显式配置 `AIVERO_TOOLS_EXPORT_CREDITS` 后才启用；不改手动导出的计费行为。后续工人负责从工具参数构造既有时间线协议，并校验资产归属、时长与总帧数。

本地识别当前免费；手动抠图现有接口不扣积分，对应 `AIVERO_TOOLS_CUTOUT_CREDITS=0`，后续统一执行仍由积分服务结算，不在本块新增收费。取消能力区分协作取消与服务商取消：识别请求取消不会强杀原生线程、不会提前释放推理槽。所有当前服务商均没有已确认的提交幂等支持，声明为 false。结果使用受鉴权的自有资产链接，无固定过期秒数；不把上游临时链接的有效期冒充已知值。SLA 默认是配置的执行超时，不是服务商承诺。

通过以下命令从定义重新生成部署清单；不传 Host Catalog 时得到未绑定基线，下面的表由该基线生成：

```sh
PYTHONPATH=apps/api .venv/bin/python scripts/tool_catalog.py --version unconfigured-v1
PYTHONPATH=apps/api .venv/bin/python scripts/tool_catalog.py --host-catalog /absolute/path/host-catalog.json --version tools-v1
```

显式发布使用同一命令追加 `--publish --published-by operator`，要求显式设置 `AIVERO_DB_URL`，不使用默认数据库。CLI 不读取根 config.json，也不发现凭据。


| 名称 | 输入 → 输出 | 积分 | 可取消 | 服务商幂等 | 结果链接有效期 | 可用 |
|---|---|---|---|---|---|---|
| read_canvas | — → text | 0 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 是 |
| list_canvas_elements | — → text | 0 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 是 |
| get_canvas_elements | — → text | 0 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 是 |
| inspect_image | — → text | 0 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 是 |
| read_asset | — → text | 0 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 是 |
| detect_subject | image → mask | 0 | 是（cooperative） | 否 | 仅当前识别操作；不持久化 | 是 |
| cutout | image, mask → image | 0 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 是 |
| text_to_image_preview | — → image | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：deployment_binding_missing |
| text_to_image_final | — → image | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：deployment_binding_missing |
| image_edit_preview | image → image | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：deployment_binding_missing |
| image_edit_final | image → image | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：deployment_binding_missing |
| text_to_video_preview | — → video | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：deployment_binding_missing |
| text_to_video_final | — → video | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：deployment_binding_missing |
| image_to_video_preview | image → video | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：deployment_binding_missing |
| image_to_video_final | image → video | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：deployment_binding_missing |
| frames_to_video_preview | — → video | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：deployment_binding_missing |
| frames_to_video_final | — → video | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：deployment_binding_missing |
| inpaint_preview | image, mask → image | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：deployment_binding_missing |
| inpaint_final | image, mask → image | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：deployment_binding_missing |
| video_concat | video → video | 未定价 | 是（cooperative） | 否 | 无固定到期；受鉴权及资产状态约束 | 否：explicit_credit_price_required |
| outpaint | image → image | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：no_implemented_adapter |
| upscale | image → image | 未定价 | 否 | 否 | 无固定到期；受鉴权及资产状态约束 | 否：no_implemented_adapter |

## 配置

新增设置统一为 `ToolSettings` / `AIVERO_TOOLS_`：rollout_groups（按工具名配置）、max_count、min_frames、max_candidates、max_mask_bytes、max_image_pixels、recognition_timeout_seconds、local_timeout_seconds、export_timeout_seconds、max_clips、max_frames、cutout_credits、export_credits。它们可收紧现有实现的固定边界；不可通过增大配置宣称旧适配器支持更多输入。读取工具复用原注册表限制；生成的尺寸、时长与价格复用现有固定绑定。发布会把这些值冻结进工具定义，旧 Run 不读最新环境配置来改价。

## 错误归一与告警

`normalize_error(provider, code, status, operation)` 使用精确结构化代码；已知代码优先于 HTTP 状态。内容拦截一律 `blocked_by_model`，不重试、不换模型绕过；积分或账户额度不足为 `quota`，不与 429 限流混淆。HTTP 参数错误 → param_invalid，429/408 → retryable，5xx → provider_down；无法分类 → unknown 并记录 ERROR 结构化告警。`normalize_error_durable(tx, ...)` 复用块 2 的告警表，保存 event_id/run_id/task_id 上下文，可在工人事务中调用。`normalize_provider_failure` 保留现有 staged `Certainty.UNKNOWN` 语义，提交结果未知不自动重试。

映射来源：[阿里云错误码](https://help.aliyun.com/en/model-studio/error-code)、[SiliconFlow 错误说明](https://docs.siliconflow.cn/en/faqs/error-code)，以及现有 ComfyUI、Recognition、ProviderFailure 契约。SiliconFlow `20012` 是模型不存在，归入 param_invalid；未确认的数字错误码不猜测。完整精确映射由 `errors.FAILURE_RULES` 导出，下面的表也从该定义生成。

| 服务商/适配器 | 精确错误码 | 统一类型 |
|---|---|---|
| aliyun | `DataInspectionFailed` | blocked_by_model |
| aliyun | `data_inspection_failed` | blocked_by_model |
| aliyun | `InputDataInspectionFailed` | blocked_by_model |
| aliyun | `OutputDataInspectionFailed` | blocked_by_model |
| aliyun | `InvalidParameter` | param_invalid |
| aliyun | `InvalidParameterValue` | param_invalid |
| aliyun | `MissingParameter` | param_invalid |
| aliyun | `Throttling` | retryable |
| aliyun | `Throttling.RateQuota` | retryable |
| aliyun | `QuotaExhausted` | quota |
| aliyun | `Arrearage` | quota |
| aliyun | `InsufficientBalance` | quota |
| aliyun | `ServiceUnavailable` | provider_down |
| aliyun | `InternalError` | provider_down |
| openai | `content_policy_violation` | blocked_by_model |
| openai | `moderation_blocked` | blocked_by_model |
| openai | `invalid_request_error` | param_invalid |
| openai | `rate_limit_exceeded` | retryable |
| openai | `insufficient_quota` | quota |
| siliconflow | `20012` | param_invalid |
| comfyui | `prompt_outputs_failed_validation` | param_invalid |
| comfyui | `invalid_prompt` | param_invalid |
| rembg | `no_subject` | no_subject |
| rembg | `mask_invalid` | param_invalid |
| rembg | `mask_source_mismatch` | param_invalid |
| rembg | `recognition_busy` | retryable |
| rembg | `recognition_duplicate` | retryable |
| rembg | `recognition_timeout` | retryable |
| rembg | `recognition_failed` | provider_down |
| rembg | `recognition_output_invalid` | provider_down |

## detect_subject 与 cutout

现有 `/api/cutout/detect/{id}` 与 `/api/cutout/apply/{id}` 已分开。本块复用 `Recognition.detect`、`storage.apply_mask`，新增 `detection.py` 纯结果适配层；生产识别接口也调用该层并保留原 `operationId/provider/model/urls` 字段，新增 result_type、candidate_count、granularity、candidates。仅改这一处 HTTP 封装以保证画布与未来 Agent 共用结果语义，没有修改画布交互。

`detect_subject` 是 read_only，image → candidates[*].mask。每个候选包括文字标签、confidence 与 confidence_source、mask 引用。rembg 本身只有一张合并前景灰度蒙版，所以返回“合并前景”、`confidence=null`、`confidence_source=unknown`、`granularity=combined_foreground`；不把连通区域当实例，不伪造数字置信度。真正的多主体适配器可以使用同一个 `normalize_candidates` 返回 instances，假接口已覆盖这个契约。

有效灰度 PNG 全透明 → 正常 `no_subject`、candidate_count=0、candidates=[]、积分估算为零。空字节、坏 PNG、非灰度蒙版、尺寸不符 → 明确错误，绝不当作无主体。旧 Recognition 只有 HTTP 状态，没有稳定的 no_subject 错误码；429 busy、504 timeout、502 inference failure 分别归一。生产适配层新增 `recognition_output_invalid`（附 mask_invalid/mask_source_mismatch 原因），映射 provider_down；用户提供的 mask_invalid/mask_source_mismatch 才映射 param_invalid。取消仍走原取消状态；不把取消当识别失败重试。

蒙版引用包含 source_asset_id、source_sha256、width、height、operation_id 和临时 handle；候选引用不存储图片内容。原手动接口继续返回临时 data URL，当前未实现供 Agent 执行的 handle 存储或解析服务。没有新增蒙版持久化或识别结果缓存，退出识别后不得恢复旧选区；工人块应在临时蒙版失效时走明确的重新识别流程，不能用旧 handle 猜测恢复。只读重新识别与大模型重调是不同操作。

`cutout` 输入 image + 一个 mask，输出新 image。结果引用和输入图片必须绑定同一源资产、哈希和尺寸。`validate_mask_source` 提供纯检查；实际资产范围检查、结果持久化与血缘关联仍由现有服务/工人完成，原图保留。

## 审核员第②道类型检查

`agent.admission.tool_types.review_tool_types` 消费同一份已锁定工具定义：检查参数 JSON Schema、任务 ID 与前置依赖、类型端口，以及已知结果上的引用路径/索引和分支。

```python
# 已由上游任务书显式指定引用；本模块不负责选择候选。
TypedTask(id="t2", tool="cutout", depends_on=["t1"], parameters={
    "image": {"asset_id": "source", "media_type": "image"},
    "mask": "$t1.candidates[0].mask",
})
```

上游端口 `candidates[*].mask: mask` 可以接下游 `mask: mask`。计划审核时结果尚不存在，返回执行前约束：上游必须 result_type=detected、指定索引确实存在、源图身份/哈希/尺寸一致；这些约束不是执行许可。拿到结果后再次校验，no_subject、错误类型、未声明依赖、越界索引和错误源图都会被拒绝。完整的合法性/意图审核、准入凭证以及工人端执行前重验仍是后续集成任务；现有 MediaPlan 的三道审核流程保持不变，没有通过第②道就直接执行的路径。

## 验证与范围

```sh
PYTHONPATH=apps/api .venv/bin/pytest -q tests/backend/test_tool_catalog.py tests/backend/test_tool_errors.py tests/backend/test_tool_detection.py
# 已迁移的独立测试 PG/Redis；不连接应用数据库：
AIVERO_TOOL_TEST_DB=... PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_tool_catalog_store.py
AIVERO_DURABLE_TEST_DB=... AIVERO_DURABLE_TEST_REDIS=... PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_production_api.py
AIVERO_CORE_MIGRATION_TEST_DB=... PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_core_migrations.py
# 显式选择真实模型配置；未设置时跳过，不冒充真实通过：
AIVERO_TOOL_SMOKE_ROUTES=/absolute/routes.json AIVERO_TOOL_SMOKE_CREDENTIALS=/absolute/credentials.json PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_tool_model_smoke.py
# 也可显式指定产品已配置的本机对话配置文件（不会输出密钥）：
AIVERO_TOOL_SMOKE_LOCAL_CONFIG=/absolute/media/private-agent-config/workspace-uuid.json PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_tool_model_smoke.py
npm run verify
```

真实冒烟用合成文字，导出读取、抠图、图片生成/编辑、文生/图生视频和帧合成的 schema；只检查模型接受请求并返回合法只读任务单，绝不调用这些生成工具。真实服务商生成和实际积分扣减不在本块覆盖范围。

现有服务商的旧适配层可能只保留通用 ProviderFailure 而丢弃原始业务错误码；本块不会从错误文案猜测恢复。后续工人接线时必须传递结构化 provider/code 和提交 certainty，才能使用精确映射。read_asset 是现有资产元数据接口的定义封装，未来处理器仍须复用资产归属检查；本块没有增加无范围的数据库读取入口。

### 本次验证记录（2026-10-22）

- 新增工具定义、错误映射、识别封装与类型链单元测试：71 项。
- 独立 PostgreSQL 上的目录发布、锁定旧 Run、不可变约束、并发发布与告警测试：10 项通过。
- 独立 PostgreSQL/Redis 上的现有生产 HTTP 回归：18 项通过，包括有主体/无主体、坏蒙版结构化错误、解码期间取消不返回迟到结果。
- 独立空库升级、回滚、再升级与回滚到 base：通过。
- 使用产品已配置的真实 qwen-plus 完成工具 schema 冒烟：通过，未执行生成工具；配置和密钥未加入提交。
- 分批提交快照分别执行 `npm run verify`：均通过；最终快照后端 915 passed、2 skipped。工作区原有前端及其他块的未提交改动未纳入本块提交。
