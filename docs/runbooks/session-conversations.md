# 画布多轮 Session

同一 workspace、用户、画布解析到同一活动 Session。消息先提交数据库，按 Session.sequence 排序；默认静默窗口 1.2 秒、最长收集 4 秒。调度器、主管、执行工人继续使用现有数据库、outbox、队列、积分和 MediaHost。`schedule` daemon 已接入派发与投影；独立部署也可运行 `python -m backend.workers.core_events session-dispatch`，不要用应用进程内定时器替代。

## 上线

1. 执行 `.venv/bin/alembic upgrade head`（新增 0024/0025）。重复活动 Session 或顶层 Run 会明确阻止迁移，先离线整理，不会自动合并历史。
2. 按现有工具目录发布流程发布支持 seed 的新目录；不要改写已发布版本或旧 Run 的锁。只有实际支持 seed 的 Provider 才开放重做。
3. 启动原有调度、主管、事件、生成和通知进程。会话 SSE 从数据库重放，Redis 仅作通知提示。

`SessionSettings` 的环境前缀为 `AIVERO_SESSION_`，包括 QUIET_WINDOW_SECONDS、MAX_MERGE_WAIT_SECONDS、QUEUE_LIMIT、DISPATCH_LEASE_SECONDS、RETRY_SECONDS、ARTIFACT_CANDIDATE_LIMIT、SLOW_TASK_NOTIFY_SECONDS。SSE 的 `AIVERO_SESSION_STREAM_` 控制 REFRESH_SECONDS、REPLAY_LIMIT、REPLAY_RETENTION、CLIENT_LEASE_SECONDS；摘要的 `AIVERO_SESSION_SUMMARY_` 控制 THRESHOLD_CHARS、KEEP_RECENT_MESSAGES、EXCERPT_CHARS、MAX_SUMMARY_CHARS。配置的类型及范围以对应 Settings 类为准。

## 恢复与边界

- Run.snapshot、scope、invariants 和版本锁保持冻结。用户后续输入追加修订；已提交任务保留产物，旧修订迟到的写单不能提交。没有提交的旧任务取消后，新计划可复用 t1/t2 名称。
- Session.active_run_id 是缓存；权威归属和“最多一个活动 Run”由 Run 表约束。缓存不设反向外键，避免受理消息等待工人持有的 Run 锁。
- 用户确认表单/选项独立保存为决定记录，摘要只做有界摘录，保留原消息和决定引用；不是额外的大模型摘要调用。
- 指代在代码中从有权限的资产索引和当前画布解析。歧义、缺失或超出冻结 scope 时先澄清，不猜资产。执行中的可读范围仅允许引用本 Run 已产出的可用资产。
- 编辑只追加版本；切换历史版本提交确定性工人命令，以文档 revision 和节点版本作 CAS。冲突返回错误，不覆盖远端修改。
- 完成通知为持久化站内通知，不包含邮件或系统推送。旧单 Agent 和手动生成入口仍保留。
- 浏览器断线消息按 Session、client_id、client_message_id 分条保存；恢复时沿用原幂等标识。服务端序号是多端权威顺序。

## 回归

```sh
npm run verify
# 必须显式配置已迁移的独立测试 PG/Redis；禁止生产库和真实付费凭据。
PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_session*.py tests/integration/test_block9_pg.py tests/integration/test_supervisor_pipeline.py
PYTHONPATH=apps/api .venv/bin/pytest -q tests/integration/test_production_api.py
PYTHONPATH=apps/api .venv/bin/python -m backend.evaluation.reference_cli --output /tmp/session-reference-report
PYTHONPATH=apps/api .venv/bin/python -m backend.evaluation.reliability.cli --output /tmp/session-reliability
```

指代夹具包含 43 条。规则成绩和真实 LLM 成绩分开；没有真实模型调用时 `llm_tested=false`。CI 保存指代报告和 Session 集成测试结果。多端单测覆盖离线消息、重复事件、快照复位及跨 Session 隔离；数据库回归覆盖合并、提交竞争、插话、纠正、通知和版本命令。
