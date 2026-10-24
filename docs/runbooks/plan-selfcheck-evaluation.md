# 计划自检评测

冻结源是 `tests/fixtures/plan-selfcheck-cases.jsonl`（50 条正常 + 30 条语义漂移）。`build_cases()` 只加载并校验该文件的 SHA-256，不展开模板。标签 `label_pass` 只用于评分，不进入 `render_messages`。

## 指标

漂移是正类。

- 模型 TPR / FPR 只使用解析成功的观测。fallback 留在 `model.counts` 和 `model.fallback_counts`，不进该率的分母，也不能因此把 FPR 写成 0
- 规则基线来自 `review.validate` 的意图门，分母是全部有规则结论的用例，不随模型 fallback 缩小。冻结集为 TPR 10/30、FPR 0/50
- 不完整的 live 运行仍写出 `model` 的部分率、覆盖、fallback、延迟和已知 token。`llm_tested` 只表示 80 条完整且可导出证据；不完整时为 false，并且不写 `evidence.json`
- 脚本协议和 fake provider 不是 live 质量，`model` 保持 null
- `comparison.model*` 是有效观测上的模型统计。`comparison.combined*` 在模型 fallback 时只采用规则结论，分母仍是全部规则用例

`selfcheck_max_false_block_rate` 仍是 `None`，enforce 保持关闭。完整 enforce 证据还要求 80 条 provenance=live 的观测、模板 / policy / 数据集 / route 一致，且 fallback 为 0。

## 命令

离线预检与规则基线不访问网络：

```bash
PYTHONPATH=apps/api .venv/bin/python -m backend.evaluation.selfcheck_cli --mode offline --output /tmp/plan-selfcheck-offline
PYTHONPATH=apps/api .venv/bin/python -m backend.evaluation.selfcheck_cli --mode rules --output /tmp/plan-selfcheck-rules
```

真实文本调用必须显式 `--mode llm`，并只给 `--route-id`，或同时给 `--local-config-root` 与 `--workspace-id`。没有密钥参数。`--token-budget` 是整次运行的 token 上限（默认 250000），不进入 `policy_fingerprint`。默认超时和上下文字节来自 Settings（当前 8 秒 / 24000），可以比 route 更严，不能比 route 更松。`--output-cap` 缺省等于 route `maxTokens`；小于 route 会拒绝，大于 route 时记录实际 route cap。timeout、unknown、blocked 不重试。每次调用在 await 前按请求字节、route 输出上限和 `--protocol-overhead` 预留额度。

输出目录已有文件时必须带 `--resume`。复用前核对 case、input hash、模板、`policy_fingerprint`、route、数据集和限额。对不上就停止，不覆盖、不重发。`sending` 且没有 raw 的记录保持 unknown。

`policy_fingerprint(Settings)` 由 runtime 提供，并且必须对应当次实际超时、输入字节、输出字节和截断参数。CLI 覆盖这些值会改变指纹。数据集内容哈希来自实际 case 的规范化 dump；冻结文件 SHA-256 只作为 `fixture_sha256`。`evidence.json` 只在 80 条有效 live 观测之后写出，内容是 `EnforcementEvidence` 的精确 dump，`policy_hash` 等于有效指纹。资格和快照写在 summary / manifest，不写入该文件，也不打开 enforce。

规则与模型的对照在 `comparison.ruleCaught` 和 `comparison.modelAdditional`，来自保存的审核原因和解析后的 raw，不用 `caught_by`。

评测 API `POST /api/evals/v1/plan-selfcheck` 只接受 `rules` 或 `offline`，用来冻结数据集并走现有 runner。任务类型是 `plan_selfcheck`。
