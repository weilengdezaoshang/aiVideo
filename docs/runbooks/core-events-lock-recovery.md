# 块 2 锁竞争与空值恢复修复

本次范围仅为事件与调度模块。新增迁移 `0017_event_lock_recovery`，保留块 1 的 Run/Task、原有业务执行及外键保护；没有修改审核、积分、生成、画布或前端代码。

## 文件范围

| 文件 | 改动 |
|---|---|
| `apps/api/backend/core/events/contracts.py` | 锁、领取租约、退避、告警与验收误差配置 |
| `apps/api/backend/core/events/orm.py` | 定时器重排期、领取字段；异常隔离表；告警上下文；deferred attempt |
| `apps/api/backend/migrations/versions/0017_event_lock_recovery.py` | 冻结迁移，原定时器 next_fire_at 从 due_at 回填，新增索引与约束；有未处理新状态时拒绝破坏性降级 |
| `apps/api/backend/core/events/locking.py` | 非阻塞取锁，区分不存在和被锁；隐式写锁等待上限 |
| `apps/api/backend/core/events/integrity.py` | 成功证据、异常死信、数据修复后的显式审计重放 |
| `apps/api/backend/core/events/timers.py` | 持久领取、非阻塞锁 Run、持久退避、跳过计数、告警关闭、取消重读检查 |
| `apps/api/backend/core/events/processor.py` | 保留候选标识，逐项重读检查；争用释放为 pending/deferred，不耗业务重试；attempt 编号独立于计费重试数 |
| `apps/api/backend/core/events/store.py` | 已处理通知确认、未知通知持久隔离、手动重放重读检查、诊断上下文 |
| `apps/api/backend/core/events/transport.py` | 排除终态隔离及已成功事件；发布结算非阻塞检查；receiver 隐式 FK 锁等待上限 |
| `apps/api/backend/core/events/recovery.py` | 找出无法进入正常 JOIN 领取查询的异常记录；恢复发布标记；跳过合法回执清理 |
| `apps/api/backend/workers/core_events.py` | 查询异常死信，增加 quarantine-replay，输出上下文 |
| `tests/backend/test_core_event_contracts.py` | 退避边界与不存在/被锁区分 |
| `tests/integration/test_core_events.py` | 原完成标志保留；非业务 deferred 不算重复执行 attempt |
| `tests/integration/test_core_event_lock_recovery.py` | 锁竞争、30 秒真实锁、清理/缺失、恢复与审计重放回归，供块 7 收录 |
| 本文及 `docs/runbooks/core-events.md` | 配置、原因分类、启动、验收和覆盖边界 |

## 调度与锁顺序

定时器的 due_at 永不因锁竞争提前或改写。短事务用 `FOR UPDATE SKIP LOCKED` 领取一个到期且 next_fire_at 已到、领取租约未占用的定时器，保存 token/lease 后提交。执行事务按 Run → Timer 非阻塞取锁，重读并核对领取。取不到 Run 锁时，另一个只锁 Timer 的短事务保存 next_fire_at、lock_skip_count 并释放领取。此事务绝不再反向请求 Run，避免锁顺序倒置。

候选按 next_fire_at 排序，重排期后不再占用原始 due_at 的队头；持续锁竞争也不会把健康 Run 挡在 LIMIT 后面。退避指数增长、带抖动并有上限；达到跳过阈值写 `backlog` 持久告警，不停止重试，触发后关闭此告警。重排期是传输元数据，不增加用户取消使用的 timer version；触发、取消、显式修复仍增加版本。

执行成功仍在同一事务写 outbox 事件和 fired 状态。定时领取中途崩溃则由持久租约恢复，旧 token 无法再次触发。

后台显式行锁采用 SKIP LOCKED；隐式 FK、UPDATE 和告警唯一键冲突受 lock_timeout_ms 限制，不能长时间挂住批次。消费器碰到争用会在短事务间退避，attempt 记 deferred，回执恢复 pending，业务重试计数不增加。持续争用超过短重试窗口时保留持久领取，依靠租约恢复；不会丢掉执行意图。

用户主动执行的单事件手动重放可等待并发的幂等重放请求，但受独立 admin_lock_timeout_ms 限制。这不是后台批次循环，不会阻塞其他 Run 的 daemon；达到上限需运维重试原 request-key。

## 空值原因与处置

| 原因 | 处置 |
|---|---|
| 候选查询快照过旧，竞争者已经推进游标 | 保留原 event_id/run_sequence，检查原事件成功证据；正常结束，debug，无业务重试 |
| processed 回执已清理 | 消费游标覆盖该序列，或成功 attempt 存在，即确认消费；不重新创建 pending 回执，不重发、不告警 |
| 有效领取仍由其他消费者持有 | 非阻塞识别忙；不并发执行，不按失败扣重试次数 |
| 领取已回收、token 改变或事件已隔离 | 旧领取不写新领取；debug 或返回已有终态 |
| 缺少未处理回执、游标、attempt、必需 Task，或序列关系不一致 | `core_event_quarantine` 持久异常死信和 `dead_letter` 告警；自动领取/重发排除该事件，后续事件不能越过它 |
| 已领取后缺少 Run 或事件 | 同上；异常死信标识字段不依赖 Run/Event 外键，保证缺失事实仍可记录 |
| 收到未知 event_id，包括违反先提交再发布协议而导致的不可见记录 | 无法证明成功，持久隔离后 ACK；不猜测为重复消息，也不无限重试 |
| 通知的 Run 与真实事件不匹配，或通知 JSON Schema 非法 | 原通知单独进入诊断死信后 ACK；保留可解析的 id 和前因，不保存非法业务载荷，不隔离或修改真实事件 |
| 重放时依赖游标、回执或死信记录缺失 | 分类隔离；已处理事件继续拒绝重放；修复前不能重新执行 |
| 定时器已领取后记录或 Run 消失 | 定时器异常死信；已有定时器进入 quarantined，停止自动触发，须显式修复 |
| 没有候选事件或定时器 | 正常空队列，不属于记录异常 |
| 直接 API 的未知 timer_id、非法参数 | 明确输入错误，尚无待确认的消费消息；不伪造成功 |
| DB 连接异常、语句/锁超时 | 这是读取/提交失败，不是空值；保留 broker requeue、领取租约和恢复机制；不能在死信未落库时 ACK |

异常死信包含 event_id、run_id、causation_id、timer_id（适用时）、phase 和明确 code。无法获知的前因标记 null。告警使用既有 dead_letter/backlog 指标和规则，不修改其他块的监控配置。

## 同类问题修复

1. 消费器 claim、execute、defer 和失败结算都检查重读结果。失效快照不再去访问不存在的“当前下一条”回执。
2. 定时器取消重读结果不再直接访问 version；背景领取、执行与重排期避免等待忙行。
3. 发布器结算先非阻塞锁定并检查事件；receiver 的 FK 检查也可能等锁，现受配置上限控制，失败重新入队。
4. 回执清理后，发布器原本会按“已发布但无回执”反复重发；现在用持久成功证据排除。恢复扫描也完成崩溃前未写回的发布标记。
5. 恢复扫描不再先把成功且已清理的回执记录填满 LIMIT，再逐个跳过；成功 attempt 不参与异常缺回执候选扫描。
6. 无效通知单独保存在 claim 命名空间，避免其使真实事件永久隔离或错误关闭真实 attempt；只有 event 命名空间的终态才阻止原事件执行。
7. 目前块 2 没有心跳业务处理器；现有 watchdog 及心跳事件依赖的 timer 已按上述方式处理。没有新增后续块业务逻辑。

## 配置与运维

原 `AIVERO_CORE_EVENTS_` 配置保留，新增：

| 后缀 | 默认值 |
|---|---:|
| LOCK_TIMEOUT_MS | 5 |
| ADMIN_LOCK_TIMEOUT_MS | 1000 |
| LOCK_RECOVERY_ATTEMPTS / LOCK_RECOVERY_DELAY_SECONDS | 5 / 0.01 |
| TIMER_LEASE_SECONDS | 60 |
| TIMER_BACKOFF_BASE_SECONDS / TIMER_BACKOFF_MAX_SECONDS | 0.1 / 5 |
| TIMER_BACKOFF_JITTER | 0.2（必须小于 1，避免零延迟空转） |
| TIMER_SKIP_ALERT_THRESHOLD | 20 |
| TIMER_ACCURACY_SECONDS | 1（验收误差配置，不是抢占系统调度的硬实时保证） |

先升级至迁移 head，再启动原 publisher/receiver/processor/timer/recovery 五个角色。已有定时器 next_fire_at 回填为原 due_at。

```sh
PYTHONPATH=apps/api .venv/bin/alembic upgrade head
# list 同时返回普通死信与异常死信，含 quarantine_id。
PYTHONPATH=apps/api .venv/bin/python -m backend.workers.core_events list
PYTHONPATH=apps/api .venv/bin/python -m backend.workers.core_events inspect EVENT_UUID
# 先恢复依赖与一致性，已成功事件永不允许重放。
PYTHONPATH=apps/api .venv/bin/python -m backend.workers.core_events quarantine-replay QUARANTINE_UUID \
  --operator OPERATOR --reason '修复说明' --request-key UNIQUE_KEY
```

已知事件异常重放使用原 EventReplay 审计，并开启新 generation；定时器异常修复要求 Run、原定时器及前因存在，且查不到已触发产物，审计保存在异常记录 context.replays 中，与重新排期同事务提交。重复 request-key 幂等。普通死信原 replay 命令保留。

```sh
# 必须使用专用且已迁移的数据库与独立 RabbitMQ；不要与其他套件共用 outbox。
AIVERO_CORE_EVENTS_TEST_DB=... AIVERO_CORE_EVENTS_TEST_BROKER=... \
.venv/bin/pytest -q tests/backend/test_core_event_contracts.py \
  tests/integration/test_core_events.py tests/integration/test_core_event_lock_recovery.py
# 另设空库 AIVERO_CORE_MIGRATION_TEST_DB 跑迁移往返。
npm run verify
```

## 覆盖边界

- Run 删除测试分两层：活跃 handler 的真实 DELETE 被行锁上限拒绝，非活跃时被现有 FK 拒绝；用局部读取故障注入验证 Run 异常缺失时的持久死信。没有禁用生产 FK 或放开 Event 不可删除约束，也没有把“不允许删除”误报为“删除成功”。
- 实际持有 Run A 行锁 30 秒；同时锁 5 个 Run 的独立用例；健康 Run 触发时间与 due_at 比较，按配置允许误差断言。
- 实际 PostgreSQL 竞争快照、回执删除、游标/attempt 删除、定时行锁、broker 实际发送后的 SIGKILL 均覆盖；事件/Run 非法缺失用故障注入覆盖，未模拟关闭 FK 后的物理数据库破坏。
- 不覆盖长期网络分区、PostgreSQL 主从切换、跨地域时钟/存储故障或硬实时 SLA；数据/告警存储自身不可用时保留消息，恢复后再落终态。
- 未引入自动清理策略。已处理回执可清理的依据是成功 attempt 或消费游标仍被保留；两者与回执同时删除后，无法证明成功，必须异常隔离。
- 后续心跳业务判断、生成/扣费对账不在本次范围；外部副作用的幂等保证仍由工人模块实现。

## 本次验收结果

2026-10-21，在单独创建的 PostgreSQL 16 / RabbitMQ 3.13 / Redis 7 测试容器中验证，未连接既有开发栈或生产数据库：

- 块 2 全部事件契约、原有完成标志及新增锁/空值回归：**34 passed**，包含真实 30 秒 Run 行锁和 5 个 Run 同时持锁。
- 块 1 PostgreSQL 数据契约/状态机回归：**62 passed**。
- 迁移空库 head → 0014 → head → base 往返：**1 passed**，包含新增 0017。
- 原生产 API 集成回归：**14 passed**。
- 最新 `npm run verify` 全流程通过，后端 **844 passed、2 skipped**；真实数据库/RabbitMQ 的块 2 回归已按专用环境另行运行。
- Ruff 与 diff 空白检查通过。

本次未执行线上迁移、部署或创建提交。测试容器验收后清理，原开发容器保持原样。
