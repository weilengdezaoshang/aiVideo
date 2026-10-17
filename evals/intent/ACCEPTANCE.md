# 意图接入验收记录

范围为当前工作区的 8443 Python Host，不使用实验性的 `apps/server` 路由。两个主开关默认均为 false。

| 门 | 代码与固定夹具验证 | 证据 |
| --- | --- | --- |
| E01–E08 | 通过：弱动态、风格义、否定、故障降级、能力关、L2 锁与缺图结构门 | `tests/backend/test_intent_pipeline.py` |
| E09–E10 | 通过：原快照、选项短路、唯一文字解析、过期与内容变更拒绝 | 单测及 `tests/integration/test_intent_host.py` |
| R01–R03 | 通过：原 Host、选区/@、旧路径与默认顺位关闭 | `test_host_request_context.py`、原 Host 与生产 API 集成测试 |
| A01–A03 | 通过：候选事务落库、不自动批准、成功稳定期与撤销复核 | feedback 单测及 Host 集成测试 |
| A04–A07 | 通过：清洗、显式 Approve、版本递增、批量索引、回滚、内置/额外评测泄漏拒绝 | `tests/backend/test_intent_feedback.py`，使用固定测试向量 |
| O01–O03 | 通过：Trace 字段、CI 固定安全集、README 的侧车/脚本/开关/回滚说明 | 固定安全集及全量 verify |
| O04 | **固定安全集通过；生产标定待完成** | `safety-baseline.json`：改图↔视频误判旧路径 2/4，新路径 0/4 |
| S01–S06、S08 | 通过：默认关、白名单、最多两步、弱顺位确认、逐步能力检查 | `test_intent_pipeline.py` |
| S07 | 通过：真实 PostgreSQL 失败注入后后步不入队，Trace 标记第 0 步失败 | `tests/integration/test_intent_host.py` |
| 多步成功 | 通过：文生图产物 → 后步参考 → 关键帧 → 有效 MP4；过程按语义步骤分组 | Host 集成测试与 `canvas-intent-steps.test.tsx` |

`exemplars.yaml` v0.2.0 全部保留 pending，没有把文档样本批量认定为金牌。`sequential-cases.json` 对齐产品案例册的 58 条编号和原始期望，仅供评测。`heldout.json` 是固定回归隔离清单，禁止通过批准操作偷偷吸收为训练句。

**尚不能宣称生产 P0 门全部绿或开启灰度**：需要人工逐条审校种子库，固定真实本地模型，重索引并对独立语义集标定阈值、记录误判及拒识分母。固定测试向量和故障降级集不能替代这一步。新版本应先 shadow 再灰度，P0 通过后才允许开启多步。

所有数据库验证使用本任务新建的独立 PostgreSQL/Redis 容器、单独端口和迁移；不连接运行中的 8443 开发数据库，不调用真实付费 Provider。

## 2026-10-17 审查问题回归

- 否定视频的「不要输出」「不要生成任何」保持图片路径；否定动画风格不阻止明确视频目标，正反视频要求冲突时澄清。
- 否定抠图不设置 matting 槽位，确认改图后保留衣服编辑计划；其他否定分句之后的正向去背景仍可识别。
- 顺位澄清选择「先做第一步」仅校验已确认的单步文本，正确交接画布抠图。
- 新完整创作指令不附带旧澄清 ID；短答和选项仍绑定原快照；「作为新需求发送」允许明确替换包括过期票据在内的旧澄清。

上述行为由 `test_intent_pipeline.py`、`canvas-agent-store.test.ts`、`canvas-agent-panel.test.tsx` 及 `tests/integration/test_intent_host.py` 的新增回归覆盖；保持原有灰度开关默认值及 Approve 前置要求。
