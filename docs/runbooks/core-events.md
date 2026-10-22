# Core 事件协作（块 2）

本模块只提供传输、串行消费、事务去重、重试、死信和定时器，不注册业务处理函数，不调用模型、生成接口、积分或画布。复用现有 PostgreSQL、Kombu/RabbitMQ `aivideo.jobs` 交换机，新增独立的 quorum 分区队列，不改变旧生成队列和 SSE 通路。数据库迁移为 `0016_core_events`，依赖块 1 的 `0015`。

## 运行

先以既有迁移入口升级数据库。每个角色独立启动，可扩容同一角色：

```sh
export PYTHONPATH=apps/api
# 使用部署环境配置的 AIVERO_DB_URL、AIVERO_BROKER_URL，不写入仓库。
.venv/bin/alembic upgrade head
.venv/bin/python -m backend.workers.core_events publisher
.venv/bin/python -m backend.workers.core_events receiver
.venv/bin/python -m backend.workers.core_events processor
.venv/bin/python -m backend.workers.core_events timer
.venv/bin/python -m backend.workers.core_events recovery
```

SIGTERM/SIGINT 停止领取并等待当前 tick 完成；SIGKILL 后租约和数据库事务负责恢复。所有角色必须连接同一数据库；publisher/receiver 必须连接同一 RabbitMQ vhost，配置相同的分区数。部署前启动全部角色；本块没有自动启用新业务入口或替换现有 Agent Worker。

## 事实与原子性

- `core_event_outbox` 保存不可变事件、`run_sequence`、`causation_id`；原有状态事件与新增领域事件共用 Run 序列。领域事件类型覆盖约定的 18 种，payload 仅允许 UUID id，数据库触发器也检查。根创建事件无前因；其他状态事件缺省承接 Run 的上一事件；领域事件必须显式给出前因或继承当前处理事件。
- 发布器短事务 `SKIP LOCKED` 领取，事务外等待 RabbitMQ 确认，再凭 token 标记。消息体严格为 `event_id/run_id`，持久化、mandatory、publisher confirm；发送后标记前崩溃允许重发。失败无限退避重试，不因达到处理器重试上限而删除通知。
- receiver 读真实事件并核对 Run，写 `core_event_receipts` 与 `core_run_consumption`，提交后 ACK。`event_id` 唯一键吸收重复投递。无效载荷记录告警后拒绝；暂时数据库失败重新入队。
- SHA256(run_id) 稳定分区。真正顺序由数据库的下一 `run_sequence` 决定，接收乱序时等待前序收件箱。不能以 broker 到达顺序推进，也不能跳过死信。
- processor 先事务持久化 attempt/token/lease，再在持有 Run 行锁的事务中运行处理函数。拿锁后重新检查候选条件，避免 READ COMMITTED 联表快照导致误回收。处理函数的数据库变更、派生事件、去重成功标记、游标推进同时提交或回滚。
- 运行事务一直持有 Run 锁，即使租约到期也不能被另一个进程接管。崩溃释放锁、回滚效果；过期 attempt 记 abandoned，并计入重试上限。过期 token 不能更新新 attempt。不同 Run 可并行。
- handler 失败先回滚，再独立事务保存退避/死信；处理中再次崩溃仍按过期 attempt 恢复。缺少处理器或版本不匹配进入 `waiting_handler`，不消耗重试次数。处理器版本在首次领取时冻结，正常重试使用相同版本；审计重放开启新 generation 后允许使用修复版本。
- 定时器先锁 Run 再锁自身，数据库时钟判断到期；写事件和标记 fired 同一事务。重启不丢，重复 tick 不重复生成；取消使用 version CAS。`expected_version` 是提供给后续业务处理器的超时判断依据，定时器本身不判断业务是否完成。

## 处理函数契约

用可信本地模块注册，环境配置示例 `AIVERO_CORE_EVENTS_HANDLER_MODULES='["backend.some_module"]'`。模块导出 `register(registry)`：

```python
from backend.core.events.contracts import EventType

async def handle(tx, context):
    # context.event 是数据库事件；context.run/task 是当前数据库状态。
    # tx.execute/add/flush/emit/transition/schedule/cancel_timer 均属于同一事务。
    await tx.emit(EventType.HELPER_REPORT, context.run.id)

def register(registry):
    registry.register(EventType.USER_MESSAGE, "v1", handle)
```

一个事件类型只能注册一个异步处理器；多个订阅者由一个处理器在同一事务显式编排。技术状态事件只做内部记账。处理器不是模型工具，模块必须是可信程序代码，只能写数据库，禁止网络、模型、生成、扣费与其他不可回滚副作用；需要工人动手时先写执行意图与 outbox。事务包装器禁止独立提交、回滚和显式事务/DDL 控制，但不是对恶意 Python 插件的安全沙箱。

“只生效一次”的范围是本 PostgreSQL 事务内的效果。后续工人调用外部服务仍必须实现提交登记、服务商幂等/收据对账；本块不能替未接入的付费接口承诺端到端恰好一次。

## 死信、因果链与告警

```sh
.venv/bin/python -m backend.workers.core_events list
.venv/bin/python -m backend.workers.core_events inspect EVENT_UUID
.venv/bin/python -m backend.workers.core_events replay EVENT_UUID \
  --operator your-identity --reason '修复原因' --request-key UNIQUE_REQUEST_KEY
```

inspect 包含处理尝试与有界因果链（跨 Run 也可追溯，超出配置深度明确标记 truncated）。死信保留原事件和序列，并阻塞该 Run；修复后只能重放死信队首。重放保留 event_id、生成审计记录、清零当前 generation 重试计数；同一个 request-key 幂等，已处理事件禁止重放。CLI 属于运维权限，依赖数据库凭据及部署访问控制，不开放无鉴权 HTTP 管理端点。

`core_event_alerts` 是持久告警记录，发布失败达到阈值、缺失 handler、死信、无效通知、积压都可查询；日志同时输出事件 id。现有 `/metrics` 增加 `aivideo_core_outbox/receipts/timers/alerts` 等有界指标，`deploy/alerts.yaml` 增加告警规则。发布恢复、handler 恢复、重放及积压消失会关闭相应告警；invalid_claim 留给运维排查。需按既有监控方式加载规则，未自动变更线上监控。

## 配置

全部运行调节值来自 `EventSettings`，环境前缀 `AIVERO_CORE_EVENTS_`：

| 配置后缀 | 默认值 | 用途 |
|---|---:|---|
| PARTITIONS / CONCURRENCY | 32 / 16 | Rabbit 分区、单 processor 并发 |
| BATCH_SIZE / PREFETCH | 100 / 32 | 单 tick 批量、broker 预取 |
| PUBLISH_LEASE_SECONDS | 60 | 发布领取租约 |
| DELIVERY_VISIBILITY_SECONDS | 120 | 已发布但无收件回执时重新通知 |
| PROCESSING_LEASE_SECONDS | 60 | 崩溃处理领取恢复 |
| HANDLER_TIMEOUT_SECONDS / STATEMENT_TIMEOUT_MS | 30 / 30000 | 处理函数/数据库语句上限 |
| MAX_ATTEMPTS | 5 | 每个 generation 的处理尝试上限，含 abandoned |
| BACKOFF_BASE_SECONDS / BACKOFF_MAX_SECONDS / BACKOFF_JITTER | 1 / 300 / 0.2 | 指数退避、上限、抖动比例 |
| POLL_SECONDS / MISSING_HANDLER_POLL_SECONDS | 1 / 10 | 轮询间隔、缺失版本等待 |
| BROKER_TIMEOUT_SECONDS | 10 | broker 建连/发布确认超时 |
| PUBLISH_ALERT_ATTEMPTS / BACKLOG_ALERT_SECONDS | 8 / 120 | 告警阈值 |
| ERROR_MAX_CHARS / CAUSAL_MAX_DEPTH | 2000 / 256 | 脱敏错误长度、因果查询上限 |
| HANDLER_MODULES | [] | 可信处理器注册模块 |

队列声明为 quorum，本地单节点不代表跨节点容灾；投递保证依赖 PostgreSQL/RabbitMQ 持久盘及生产备份策略。调整分区数时需协调所有发布/接收进程并清理旧分区，数据库可修复没有收件回执的丢失通知。

## 验证

仅使用本模块专用、独立可丢弃且已迁移的数据库和 broker；禁止指向生产，也不要复用其他集成套件留有未发布事件的数据库。发布器会扫描整个 outbox，故测试不能与其他事件写入套件共库并行。使用专用的 `AIVERO_CORE_EVENTS_TEST_DB` / `AIVERO_CORE_EVENTS_TEST_BROKER` 显式启用：

```sh
# 显式设置独立 AIVERO_DB_URL 后先 alembic upgrade head。
AIVERO_CORE_EVENTS_TEST_DB="$AIVERO_DB_URL" \
AIVERO_CORE_EVENTS_TEST_BROKER="$AIVERO_BROKER_URL" \
.venv/bin/pytest -q tests/backend/test_core_event_contracts.py tests/integration/test_core_events.py
# 迁移往返测试必须另设空库 AIVERO_CORE_MIGRATION_TEST_DB。
npm run verify
```

集成测试包含：100 个同 Run 事件乱序并发重复送达、10 个 Run 同时处理、发送确认后标记前真实 SIGKILL、数据库效果只提交一次、失败效果/派生事件回滚、死信阻塞和并发幂等重放、缺失处理器不耗重试、崩溃领取恢复与旧 token 拒绝、活跃事务租约过期不重入、定时器新进程恢复及取消 CAS、持久告警和指标。不会调用真实生成接口。

迁移回退不会静默丢数据：存在领域事件、未处理收件箱或未触发定时器时拒绝降级，应先备份并按停机方案处理。空库 head ↔ base 可往返。

## 本次验收记录（2026-10-21）

在本地专门创建的 PostgreSQL 16、RabbitMQ 3.13、Redis 7 容器中验证，未使用现有开发栈数据库或生成凭据：

- 事件契约与真实基础设施故障测试：14 passed。
- 块 1 PostgreSQL 状态机/数据契约：62 passed。
- 空库升级、回滚与再升级：1 passed。
- 原生产 API、Host Media、审核准入、Agent API、画布执行/竞态/治理：74 passed。
- `npm run verify` 全流程通过，其中后端 841 passed、2 skipped；事件 PostgreSQL/RabbitMQ 测试以上述显式环境另行运行。
- Ruff、diff 空白检查、告警规则 YAML 解析通过。

测试基础设施验收后已清理；尚未执行线上迁移、启动新 daemon 或加载线上告警规则。

## 锁竞争与空值恢复修复

后续迁移 `0017_event_lock_recovery` 修复定时器队头饥饿与消费候选快照竞争，补齐关联记录缺失的持久异常死信、审计恢复和合法回执清理处理。新增配置、改动文件、逐项空值分类、回归入口及覆盖边界见 [块 2 锁竞争与空值恢复修复](core-events-lock-recovery.md)。部署应升级到当前 head。
