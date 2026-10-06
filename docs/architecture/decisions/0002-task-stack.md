# ADR-0002:任务执行与持久化选型(Celery + RabbitMQ + PostgreSQL)

状态:已接受(补充要求 §二 明确选型,替代原任务"数据库领取或成熟任务框架二选一")。

## 决策

- 业务任务执行:Celery(prefork 池,执行模型见 ADR-0003 待写)。
- 消息代理:RabbitMQ(业务任务消息);Redis 仅做缓存/通知/限流协调,不承担任务消息。
- 持久化:PostgreSQL + SQLAlchemy 2.x + Alembic,驱动 psycopg3(同一驱动覆盖
  Celery 同步与 FastAPI 异步两种执行模型,避免双驱动)。
- 数据库驱动兼容性结论(§二.5):psycopg3 同步连接在 SQLAlchemy 2.0.52 下完成
  0001 迁移与事务测试;其 async 能力留待 API 切换时验证,不据此宣称。

## 现状与迁移路径(§二.4:不静默偏离)

现有系统:单进程 FIFO 内存队列(jobs.py,fcntl 锁强制单实例)+ JSON 文件存储
(storage.py)。全部 193 项测试依赖该形态,直接删除代价显著。

分阶段迁移,不永久双写:

1. 阶段 A(本批):PG Schema 落地(ORM+Alembic 0001),业务写入仍走 JSON 路径。
2. 阶段 B:Celery 执行契约隔离验证(prefork + asyncio 事件循环 + 客户端生命周期),
   产出 ADR-0003 与可运行验证;JSON→PG 的一次性导入脚本(dry-run/校验/回滚)。
3. 阶段 C:API 受理切换为"PG 事务 + Outbox + RabbitMQ 发布确认",JSON 存储降级为
   只读兼容层;FIFO 队列删除。切换以闭环测试(§十四.3)为门禁。

## 兼容性约束

- JSON 文件目录布局与文档/历史格式保持只读兼容(原任务 §十一.10);
- 前端契约(202+jobId、SSE、错误响应)不变;
- 单实例 fcntl 锁在切换完成前保留,防止旧新队列并行写同一数据目录。
