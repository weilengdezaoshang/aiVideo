# ADR-0003:Celery 与 async Provider 的执行契约(隔离验证结论)

状态:已接受。基于 tests/backend/test_celery_contract.py(7/7 通过,真实 Celery worker +
真实 fork + 真实 PostgreSQL)的验证证据。

## 决策

### 1. 执行池:prefork(生产)/ solo(测试与本地)

- prefork 提供进程隔离与崩溃回收(`worker_max_tasks_per_child=100` 定期重建子进程);
- 测试用 celery.contrib.testing 的进程内 worker(solo 池 + memory broker),
  不需要真实 Broker 即可验证任务编排契约;
- 池级与 Broker 级行为(多 worker 并发、失联重投实测)在 §十五.3/4 闭环中验证,
  本 ADR 只断言配置契约(acks_late、reject_on_worker_lost、prefetch=1、持久化消息)。

### 2. 同步任务入口 → async 应用服务:AsyncRuntime

- 每个 Celery 进程一个 `AsyncRuntime`(backend/workers/runtime.py):
  专属守护线程驱动独立事件循环,`run(coro, timeout)` 调度,循环**跨任务复用**
  (验证:两次任务的 loop id 相同);
- task 函数保持轻量:取 runtime → 调 async 用例(backend/workers/usecases.py),
  业务规则不在 task 内;
- **禁止** `asyncio.run()` 每任务新建循环(会连带废弃循环绑定的客户端,§四禁止项)。

### 3. 客户端生命周期(§四:HTTPX/数据库/Redis 归属)

- HTTPX AsyncClient 与 SQLAlchemy 异步引擎**属 runtime 所有**,首次使用时在
  循环线程内创建(线程断言强制),`close()` 在循环内 aclose/dispose(验证:同一
  client id 复用;closed 后调度失败);
- Redis 客户端(§十五.5 引入)按同一模式:runtime 持有、循环内使用、close 释放;
- Session 每事务创建(UoW),不跨并发任务共享。

### 4. fork 隔离(验证:fork 探针)

- fork 后子进程中,父进程 runtime **不可用**:驱动线程不复存在(调度超时)、
  客户端线程断言拒绝使用;子进程**自建** runtime 后一切正常(exit 0);
- 因此 `worker_process_init` 钩子在子进程预建 runtime;首任务懒建为兜底(solo 池
  无此信号);
- 父进程不在 import 期创建任何连接(DatabaseSettings 仅配置,引擎按需创建),
  满足 §三.2"父进程连接池不得复用到 fork 后子进程"。

### 5. 配置传递:固化进 celery conf(验证中发现的坑)

- **证据**:monkeypatch 设置的环境变量在池子进程内不可见(探针记录 envUrl=None、
  pid 与主进程不同)——环境变量继承时机随池实现而变,不可依赖;
- 因此数据库 URL 在 app 构造时固化进 `celery_app.conf.aivideo_database_url`,
  任务从 conf 读取;生产部署由 worker 启动环境(AIVERO_DB_URL)注入;
- Redis/Broker URL 遵循同一模式(§十五.5)。

### 6. 协作取消与硬超时(§四:取消语义)

- 协作取消:业务取消以**结构化标记** `jobs.recovery='abandon'` 表达,阶段检查点
  在一切派发动作前检查(验证:取消任务的阶段执行返回 cancelled=true 且
  `upstream_cancelled=None` —— 不宣称上游已取消);
- **Celery revoke/terminate 不视为上游取消**:revoke 只影响本 Broker 消息,
  上游付费任务取消必须经 provider.cancel_external(§六/原任务已知问题 #4 语义);
- 硬超时:runtime.run 超时即取消协程且循环保持可用(验证);池级
  `task_soft_time_limit=1500 / task_time_limit=1800` 作为纵深防御。

## 已知限制

- 本验证在 macOS 本机(真实 fork + 真实 worker)完成;Linux CI 需复跑
  test_celery_contract.py(prefork 时序差异)。
- RabbitMQ 发布确认、quorum queue、消费重投实测属 §十五.3 闭环;Redis 客户端
  生命周期实测属 §十五.5。
- `worker_max_tasks_per_child=100` 与池大小为初始值,压测后调整(§八)。
