"""Outbox→缓存/通知接线与跨实例通知桥(补充要求 §十.3/§十.12)。

数据库关键状态提交后经 Outbox 可靠更新缓存,并以 Redis 频道发布通知供多实例
SSE 桥接(§十.3/§十.12);通知不携带媒体与敏感参数。
"""

import asyncio
import json
import os
import socket
import time
import uuid

import pytest

from backend.infrastructure.database import DatabaseSettings
from backend.infrastructure.uow import AsyncUnitOfWork

TEST_DB_URL = os.environ.get(
    "AIVERO_TEST_DB_URL",
    "postgresql+psycopg://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test",
)
PG_URL = os.environ.get(
    "AIVERO_TEST_PG_URL", "postgresql://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test"
)
BROKER_HOST = os.environ.get("AIVERO_TEST_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("AIVERO_TEST_BROKER_PORT", "5673"))
BROKER_URL = f"amqp://aivideo:aivideo-test@{BROKER_HOST}:{BROKER_PORT}//"
REDIS_URL = os.environ.get("AIVERO_TEST_REDIS_URL", "redis://127.0.0.1:56379/0")


REDIS_HOST = os.environ.get("AIVERO_TEST_REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("AIVERO_TEST_REDIS_PORT", "56379"))


def _reachable(host, port):
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _pg_available():
    import psycopg

    try:
        with psycopg.connect(PG_URL, connect_timeout=3):
            return True
    except Exception:
        return False


requires_all = pytest.mark.skipif(
    not (
        _reachable(BROKER_HOST, BROKER_PORT)
        and _reachable(REDIS_HOST, REDIS_PORT)
        and _pg_available()
    ),
    reason="PG/RabbitMQ/Redis 隔离实例不可用,集成测试未执行(不视为通过)",
)


@pytest.fixture()
def clean_all():
    from sqlalchemy import create_engine

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        for table in ("outbox", "generation_requests", "jobs", "assets"):
            conn.exec_driver_sql(f"DELETE FROM {table}")
    engine.dispose()
    import redis

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield


def seed_running_job() -> tuple[uuid.UUID, uuid.UUID]:
    """建一个 running 任务并在同事务提交其 completed 终态所需的 Outbox 事件。

    模拟管线终态路径:任务转 completed + Outbox job.updated(安全快照 payload)。
    """

    async def seed():
        from backend.services.pipeline import DEFAULT_SLOT_SCOPE, project_snapshot

        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            workspace_id = uuid.uuid4()
            await uow.workspaces.ensure(workspace_id)
            job = await uow.jobs.create(workspace_id=workspace_id, kind="image", params={})
            job_id = job.id
            await uow.jobs.transition(job_id, {"queued"}, "running")
            await uow.jobs.set_poll_schedule(
                job_id, external_task_id="stub-notify", next_poll_at=None
            )
            await uow.jobs.try_acquire_slot(job_id, scope=DEFAULT_SLOT_SCOPE, limit=5)
            assert await uow.jobs.transition(job_id, {"running"}, "completed") is True
            await uow.jobs.release_slot(job_id)
            refreshed = await uow.jobs.get(job_id)
            await uow.session.refresh(refreshed)  # UPDATE(onupdate) 后显式重载,避免 lazy IO
            snapshot = project_snapshot(refreshed, asset_ids=["a-1"])
            await uow.outbox.enqueue(
                aggregate_type="job",
                aggregate_id=str(job_id),
                event_type="job.updated",
                payload={"snapshot": snapshot},
            )
            await uow.commit()
            return workspace_id, job_id

    return asyncio.run(seed())


@requires_all
def test_outbox_event_updates_cache_and_publishes_notify(clean_all):
    """事件消费后:缓存出现终态快照;Redis 频道发布同一通知(§十.3/§十.12)。"""
    from kombu import Connection

    from backend.infrastructure.job_cache import (
        JobCache,
        JobCacheSettings,
        apply_event_with_backfill,
    )
    from backend.infrastructure.messaging import declare_topology
    from backend.infrastructure.notify import RedisNotify
    from backend.infrastructure.outbox_dispatcher import OutboxDispatcher
    from backend.workers.consumer import drain_generation_events

    workspace_id, job_id = seed_running_job()

    import redis

    cache = JobCache(
        redis.Redis.from_url(REDIS_URL, decode_responses=True),
        JobCacheSettings(key_prefix="aivideo-test"),
    )
    notify = RedisNotify(REDIS_URL, channel="aivideo-test:notify:jobs")

    def db_reader(workspace_id, job_id):
        """生产消费者语义:事件应用失败时从数据库重新投影(权威状态)。"""

        async def read():
            from backend.services.pipeline import project_snapshot as _project

            async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
                job = await uow.jobs.get(uuid.UUID(job_id))
                await uow.session.refresh(job)
                return _project(job, asset_ids=["a-1"])

        return asyncio.run(read())

    def on_message(body, message):
        if body["eventType"] == "job.updated":
            snapshot = body["payload"]["snapshot"]
            apply_event_with_backfill(cache, snapshot, db_reader)
            notify.publish(
                {
                    "eventId": body["eventId"],
                    "jobId": snapshot["jobId"],
                    "workspaceId": snapshot["workspaceId"],
                    "status": snapshot["status"],
                    "stateVersion": snapshot["stateVersion"],
                }
            )
        message.ack()

    # Pub/Sub 不保留历史:必须先订阅再触发发布
    sub = notify.subscribe()

    assert OutboxDispatcher(DatabaseSettings(url=TEST_DB_URL), BROKER_URL).dispatch_once()[
        "published"
    ] == 1

    with Connection(BROKER_URL) as conn:
        declare_topology(conn)
        def on_error(body, exc):
            import traceback as _tb

            _tb.print_exception(type(exc), exc, exc.__traceback__)

        drained = drain_generation_events(
            conn, on_message, limit=1, timeout_s=10, on_error=on_error
        )
    assert drained == 1

    result = cache.get_snapshot(str(workspace_id), str(job_id))
    assert result is not None and result["status"] == "completed"
    assert result["assetIds"] == ["a-1"]

    received = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not received:
        message = sub.get_message(timeout=1.0)
        if message and message.get("type") == "message":
            received.append(json.loads(message["data"]))
    assert received and received[0]["status"] == "completed"
    assert "snapshot" not in received[0], "通知只携带标识与状态,不携带完整快照与媒体"
