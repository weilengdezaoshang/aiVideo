# 缺口矩阵(补充要求 §一/§十五)

> 生成日期:2026-09-13。状态:✅已实现且已验证 / 🟡已实现未验证 / 🔶部分实现 / ⬜尚未实现。
> 本矩阵随阶段推进更新;验证=真实隔离实例或真实浏览器上的可重复测试,不含 mock 全通过。

## 基础组件选型(§二)

| 项 | 状态 | 证据/说明 |
| --- | --- | --- |
| FastAPI + Uvicorn | ✅ | 生产运行中,fastapi 0.141.1/uvicorn 0.52.4(锁定) |
| Pydantic 请求/响应/事件模型 | 🔶 | GenParams 等参数模型已有;公开响应模型与事件 Schema 未建模(§十二) |
| pydantic-settings 类型化配置 | 🔶 | 数据库配置已接入;其余 config.json 项未迁移 |
| SQLAlchemy 2.x ORM | ✅ | 8 表模型+仓库+UoW;真实 PG 16 上迁移/漂移/原子性/乐观并发 5/5 通过 |
| Alembic 迁移 | ✅ | 手写 0001(经审查);base↔head 每用例升降级;漂移检测把关 |
| PostgreSQL 持久化 | 🟡 | Docker postgres:16-alpine 隔离实例 |
| Celery 后台任务 | 🔶 | 执行模型契约已验证(ADR-0003,runtime/循环/客户端/fork/取消 7 项);业务管线与 Broker 闭环待 §十五.3/4 |
| RabbitMQ 消息代理 | ✅ | 3.13-alpine 真实实例;投递闭环 7/7(确认发布/无路由/退避/多投递器/死信) |
| Redis + redis-py | ✅ | redis:7-alpine 真实实例;任务状态缓存 8 项契约全过(§十,见 tdd-progress 第 11 轮) |
| HTTPX 外部请求 | ✅ | Provider 共享 AsyncClient,超时与脱敏已有 |
| 统一结构化日志/异常/追踪 | 🔶 | 错误契约+请求 ID+访问日志已验证;跨队列上下文传递未做 |
| 统一资产存储适配 | 🔶 | 磁盘实现已有;存储适配接口未定义 |

## 三~十三逐项(摘要)

| 要求 | 状态 | 说明 |
| --- | --- | --- |
| §三 Engine/Session/UoW/Repository/迁移 | ✅ | 异步侧完成并验证(见 tdd-progress 第 7 轮);Celery 同步侧随 §十五.2 |
| §三 连接池预算/四类超时区分 | 🔶 | 配置字段已就绪(pool_size/max_overflow/connect_timeout/pool_timeout/statement_timeout);实际预算按部署实例数待定 |
| §四 Celery+async 执行契约隔离验证 | ✅ | ADR-0003;7 项契约(真实 worker+真实 fork+真实 PG)通过;循环/客户端生命周期、协作取消、硬超时均有证据 |
| §五 Outbox/多投递器/发布确认/死信 | 🔶 | 投递侧全链路已验证(tdd-progress 第 9 轮):SKIP LOCKED 领取、确认后标记、无路由退避、滞留回收、死信可查询、消费幂等;API 受理路由挂接与消费确认超时压测属 §十五.4 |
| §六 业务状态与 Celery 状态分离 | ⬜ | state_version/execution_epoch 字段已预留 |
| §七 分阶段执行/nextPollAt/Beat | 🔶 | 分阶段管线+到期调度+退避/截止/unknown 已验证(tdd-progress 第 10 轮);Celery Beat 单调度者与周期任务接线待做 |
| §八 全局并发/公平性(100 请求/上限 5) | 🔶 | 模型名额原语已验证:咨询锁串行化领取,20 并发恰 5 提交/15 延迟/终态释放(tdd-progress 第 10 轮);100 请求多 Worker 全链路验收、速率限制与排队额度待管线接真实 Provider |
| §九 熔断/延迟重试/死信恢复 | 🔶 | 死信恢复 CLI 已验证(list/requeue/终态拒绝/审计留痕,tdd-progress 第 10 轮);失败统计熔断与按阶段延迟重试未做 |
| §十 Redis 任务状态缓存 | 🔶 | 缓存契约 8/8 + **Outbox→缓存/通知生产接线已验证**(第 12 轮:apply_event_with_backfill + RedisNotify 跨实例频道);SSE 推送端挂接归 API 切换 |
| §十一 上传/媒体资源预算 | 🔶 | 现有解码校验+尺寸限制;流式与孤儿回收测试缺 |
| §十二 OpenAPI 类型生成/schemaVersion | 🔶 | 事件 schemaVersion 白名单+未识别版本死信已验证(第 13 轮);公开响应模型与类型生成待 API 切换(阶段 C) |
| §十三 Compose 与指标 | 🔶 | compose.dev.yaml(config 校验过)+ liveness/readiness 分离 + `/api/metrics/runtime` 端点(第 13-14 轮);告警规则与生产监控栈待接 |
| §十四 ORM/迁移/集成/闭环测试 | 🔶 | 本批补 ORM 事务与迁移测试;真实 RabbitMQ/Redis 待 §十五.2 |

## 实施顺序(§十五)

1. ✅ ORM、事务和应用服务边界(异步侧;真实 PG 16 验证通过,见 tdd-progress 第 7 轮)
2. ✅ Celery 执行模型隔离验证(ADR-0003;7 项契约测试,真实 worker/fork/PG)
3. 🔶 **PG + Outbox + RabbitMQ 可靠投递闭环:投递侧已验证(tdd-progress 第 9 轮,19/19)**;
   余项:API 受理路由挂接 Outbox、消费确认超时与 quorum 行为压测、消费端重投递上限 ——
   归入 §十五.4 一起交付
4. 🔶 **分阶段任务、恢复、全局额度和死信:核心已验证(tdd-progress 第 10 轮,25/25)**;
   余项:受理 API 路由挂接(202 语义切换,ADR-0002 阶段 C)、Celery Beat 周期任务接线、
   100 请求多 Worker 全链路验收、失败统计熔断
5. ✅ Redis 任务缓存(契约 8/8 + Outbox→缓存/RedisNotify 生产接线,第 11-12 轮)
6. ✅ 权限、资产、运行指标和故障演练(核心):§八 容量验收、指标注册表、权限接口、
   旧消息兼容、readiness/liveness 分离、compose.dev.yaml、Redis/Broker 降级演练;
   **余项:OpenAPI 类型生成(待 API 切换)、指标暴露端点、真实浏览器验证**
7. ⬜ 完整回归、兼容验证和文档更新(收尾:最终报告与迁移计划)

## 集成测试执行方式

见 docs/runbooks/integration-testing.md:三容器(PG 16 / RabbitMQ 3.13 / python runner)
同处 aivideo-test 网络,在 runner 内执行 tests/integration(当前宿主 colima 对新容器
端口转发失效,宿主直连时 delivery 用例显式 skip)。
