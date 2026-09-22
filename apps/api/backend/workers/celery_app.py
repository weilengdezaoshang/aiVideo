"""Celery 应用(补充要求 §四/§五.11/§六)。

配置契约(tests/backend/test_celery_contract.py 把关):
- 不启用 result backend(§六.4):业务状态以 PostgreSQL 为唯一依据;
- acks_late + reject_on_worker_lost:worker 失联消息重投,重复投递由业务幂等消化(§五.6);
- prefetch=1:公平消费(§八);消息持久化(§五.9);
- broker_transport_options.confirm_publish:RabbitMQ 发布确认(§五.3,闭环阶段验证);
- task_time_limit/soft_time_limit 是纵深防御;协作取消走业务检查点(§四禁止
  把 revoke/terminate 等同于上游已取消)。
"""

import os

from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown, worker_shutdown, after_setup_logger

celery_app = Celery(
    "aivideo",
    broker=os.environ.get("AIVERO_BROKER_URL", "memory://"),
    include=["backend.workers.tasks", "backend.workers.durable"],
)
# 数据库 URL 在 app 构造时固化进 conf,随 app 状态进入池子进程;
# 任务从 conf 读取,不依赖子进程的环境变量继承(测试/部署语义一致,§三.1)。
celery_app.conf.update(
    aivideo_database_url=os.environ.get(
        "AIVERO_DB_URL", "postgresql+psycopg://aivideo:aivideo@127.0.0.1:5432/aivideo"
    ),
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_delivery_mode="persistent",
    broker_transport_options={"confirm_publish": True},
    # 纵深防御:runtime 层超时为主,池级硬杀兜底(prefork)。
    task_soft_time_limit=1500,
    task_time_limit=1800,
    # 子进程定期回收,限制泄漏与旧连接的存续时间。
    worker_max_tasks_per_child=100,
    # 周期调度(§七.6):单一 beat 入口;重复触发由数据库领取与幂等保证安全。
    beat_schedule={
        "dispatch-outbox": {
            "task": "ops.dispatch_outbox",
            "schedule": 2.0,
        },
        "scan-due-polls": {
            "task": "ops.scan_due_polls",
            "schedule": 5.0,
        },
        "reclaim-stale-publishing": {
            "task": "ops.reclaim_stale_publishing",
            "schedule": 60.0,
        },
    },
)


@after_setup_logger.connect
def _structured_worker_logs(**kwargs):
    if os.environ.get("SWARMUI_LOG_FORMAT") == "json":
        from backend.observability import setup_logging
        setup_logging(os.environ.get("LOG_LEVEL", "INFO"), "json")


@worker_process_init.connect
def _init_child(**kwargs):
    """prefork 子进程 fork 后自建运行时(循环/HTTPX/DB 客户端),不复用父进程资源(§三.2)。"""
    from backend.workers.runtime import get_runtime

    get_runtime()


@worker_process_shutdown.connect
def _shutdown_child(**kwargs):
    from backend.workers.runtime import peek_runtime

    runtime = peek_runtime()
    if runtime is not None:
        runtime.close()


@worker_shutdown.connect
def _shutdown_worker(**kwargs):
    """solo/线程池或主进程退出时同样释放。"""
    from backend.workers.runtime import peek_runtime

    runtime = peek_runtime()
    if runtime is not None:
        runtime.close()
