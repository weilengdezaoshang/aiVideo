"""Celery 任务入口(§四:轻量,只把运行时与应用用例拼起来;不含业务规则)。"""

import asyncio
import os
import uuid

from backend.workers.celery_app import celery_app
from backend.workers.probe import PROBE_RESULTS
from backend.workers.runtime import get_runtime


@celery_app.task(name="run_generation_step")
def run_generation_step(job_id: str) -> None:
    """执行一个生成任务阶段(轻量:用例承担全部业务规则)。"""
    from backend.infrastructure.database import DatabaseSettings
    from backend.workers.usecases import execute_generation_step

    runtime = get_runtime()
    settings = DatabaseSettings(url=celery_app.conf.aivideo_database_url)
    outcome = runtime.run(execute_generation_step(uuid.UUID(job_id), settings), timeout=60)
    PROBE_RESULTS[f"outcomes:{job_id}"] = [*PROBE_RESULTS.get(f"outcomes:{job_id}", []), outcome]
    if outcome.get("executed"):
        PROBE_RESULTS[f"exec:{job_id}"] = PROBE_RESULTS.get(f"exec:{job_id}", 0) + 1


@celery_app.task(name="probe.loop_identity")
def probe_loop_identity() -> None:
    """集成探针:记录 pid/事件循环 id/HTTPX 客户端 id 到进程内观察点。"""
    runtime = get_runtime()

    async def identity():
        return {
            "pid": os.getpid(),
            "loop": id(asyncio.get_running_loop()),
            "http": id(runtime.http()),
        }

    outcome = runtime.run(identity(), timeout=10)
    outcome["envUrl"] = os.environ.get("AIVERO_DB_URL")
    key = "loop2" if "loop1" in PROBE_RESULTS else "loop1"
    PROBE_RESULTS[key] = outcome


@celery_app.task(name="probe.cancelled_step")
def probe_cancelled_step(job_id: str) -> None:
    """集成探针:对已取消任务执行单阶段用例,验证取消检查点。"""
    from backend.infrastructure.database import DatabaseSettings
    from backend.workers.usecases import execute_generation_step

    runtime = get_runtime()
    try:
        settings = DatabaseSettings(url=celery_app.conf.aivideo_database_url)
        outcome = runtime.run(
            execute_generation_step(uuid.UUID(job_id), settings), timeout=30
        )
        outcome["envUrl"] = os.environ.get("AIVERO_DB_URL")
        outcome["pid"] = os.getpid()
    except Exception as exc:  # 探针记录失败原因,便于契约诊断
        import traceback

        outcome = {
            "error": repr(exc),
            "traceback": traceback.format_exc(),
            "envUrl": os.environ.get("AIVERO_DB_URL"),
            "pid": os.getpid(),
        }
    PROBE_RESULTS[f"cancel:{job_id}"] = outcome


@celery_app.task(name="ops.dispatch_outbox")
def dispatch_outbox_events() -> None:
    """Outbox 补投递(§五.7/§七.8):轻量调用投递器,失败记录不中断调度。"""
    from backend.infrastructure.database import DatabaseSettings
    from backend.infrastructure.outbox_dispatcher import OutboxDispatcher

    dispatcher = OutboxDispatcher(
        DatabaseSettings(url=celery_app.conf.aivideo_database_url),
        celery_app.conf.broker_url,
    )
    dispatcher.dispatch_once()


@celery_app.task(name="ops.scan_due_polls")
def scan_due_polls() -> None:
    """到期查询扫描(§七.2/§七.8):领取到期任务并派发,重复触发由幂等保证安全。"""
    from backend.infrastructure.database import DatabaseSettings
    from backend.services.pipeline import scan_due_polls as scan

    scan(DatabaseSettings(url=celery_app.conf.aivideo_database_url))


@celery_app.task(name="ops.reclaim_stale_publishing")
def reclaim_stale_publishing() -> None:
    """滞留 publishing 回收(§五.7/§七.8)。"""
    from backend.infrastructure.database import DatabaseSettings
    from backend.infrastructure.outbox_dispatcher import OutboxDispatcher

    OutboxDispatcher(
        DatabaseSettings(url=celery_app.conf.aivideo_database_url),
        celery_app.conf.broker_url,
    ).reclaim_stale_publishing(older_than_s=60)
