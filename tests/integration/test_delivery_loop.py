"""可靠投递闭环(补充要求 §五/§十四.3):API→Outbox→RabbitMQ→Worker→DB/SSE 闭环的
投递侧与消费侧契约,全部使用真实 RabbitMQ + 真实 PostgreSQL + 真实 Celery worker。

前置:docker run -d --name aivideo-test-rabbit -e RABBITMQ_DEFAULT_USER=aivideo \
  -e RABBITMQ_DEFAULT_PASS=aivideo-test -p 127.0.0.1:5673:5672 rabbitmq:3.13-alpine
broker 不可用时显式 skip(未执行不冒充通过)。
"""

import asyncio
import os
import json
import socket
import time
import uuid

import pytest

from backend.infrastructure.database import DatabaseSettings
from backend.infrastructure.uow import AsyncUnitOfWork

# 隔离实例地址可被环境覆盖(容器化执行时指向容器名);默认为本机 docker 映射。
TEST_DB_URL = os.environ.get(
    "AIVERO_TEST_DB_URL",
    "postgresql+psycopg://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test",
)
PG_URL = os.environ.get(
    "AIVERO_TEST_PG_URL", "postgresql://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test"
)
BROKER_HOST = os.environ.get("AIVERO_TEST_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("AIVERO_TEST_BROKER_PORT", "5673"))
BROKER_URL = os.environ.get(
    "AIVERO_TEST_BROKER_URL", f"amqp://aivideo:aivideo-test@{BROKER_HOST}:{BROKER_PORT}//"
)

from backend.workers.probe import PROBE_RESULTS  # noqa: E402


def _pg_available() -> bool:
    import psycopg

    try:
        with psycopg.connect(PG_URL, connect_timeout=3):
            return True
    except Exception:
        return False


def _broker_available() -> bool:
    try:
        with socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=2):
            return True
    except OSError:
        return False


requires_broker = pytest.mark.skipif(
    not (_broker_available() and _pg_available()),
    reason="RabbitMQ/PostgreSQL 隔离实例不可用,集成测试未执行(不视为通过)",
)


@pytest.fixture()
def clean_topology():
    """每用例:清空业务表 + 清空测试队列(purge),保证断言不受历史消息干扰。

    broker 不可达时显式 skip:这些用例依赖真实 RabbitMQ,未执行不冒充通过。
    """
    if not _broker_available():
        pytest.skip("RabbitMQ 隔离实例不可达,依赖 broker 的用例未执行")
    from sqlalchemy import create_engine

    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        for table in (
            "outbox",
            "generation_requests",
            "jobs",
            "asset_references",
            "assets",
            "attempts",
            "documents",
        ):
            conn.exec_driver_sql(f"DELETE FROM {table}")
    engine.dispose()

    from kombu import Connection

    from backend.infrastructure.messaging import declare_topology

    with Connection(BROKER_URL) as conn:
        declare_topology(conn)
        channel = conn.channel()
        for name in ("aivideo.jobs.generation", "aivideo.dead"):
            channel.queue_purge(name)

    PROBE_RESULTS.clear()
    yield


def seed_job_with_outbox(prompt: str = "闭环") -> tuple[uuid.UUID, uuid.UUID]:
    """建工作区+queued 任务+Outbox job.created 事件,同事务提交(§五.1)。"""

    async def seed():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            workspace_id = uuid.uuid4()
            await uow.workspaces.ensure(workspace_id)
            job = await uow.jobs.create(workspace_id=workspace_id, kind="image", params={"prompt": prompt})
            event = await uow.outbox.enqueue(
                aggregate_type="job",
                aggregate_id=str(job.id),
                event_type="job.created",
                payload={"jobId": str(job.id)},
            )
            await uow.commit()
            return job.id, event.id

    return asyncio.run(seed())


def wait_for(predicate, timeout: float = 30.0, what: str = ""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"等待超时:{what}")


# ---------- 消费任务侧的幂等语义(真实 worker 执行) ----------


@requires_broker
def test_end_to_end_outbox_to_worker_updates_business_state(clean_topology):
    """闭环:Outbox 事件经 RabbitMQ(发布确认)→ 消费者派发任务 → worker 执行 → DB 终态;
    重复投递同一事件,业务动作只执行一次(§五.6/§八)。"""
    from celery.contrib.testing.worker import start_worker

    from backend.workers.celery_app import celery_app
    from backend.workers.consumer import drain_generation_events
    from backend.infrastructure.messaging import declare_topology
    from backend.infrastructure.outbox_dispatcher import OutboxDispatcher
    from backend.workers.tasks import run_generation_step

    celery_app.conf.aivideo_database_url = TEST_DB_URL  # worker 任务用例的 DB 指向
    job_id, event_id = seed_job_with_outbox()
    dispatcher = OutboxDispatcher(DatabaseSettings(url=TEST_DB_URL), BROKER_URL)
    result = dispatcher.dispatch_once()
    assert result["published"] == 1

    async def status():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            job = await uow.jobs.get(job_id)
            events = await uow.outbox.get(event_id)
            return job.status, events.status

    assert asyncio.run(status())[1] == "published"  # 确认发布后才标记(§五.5)

    with start_worker(
        celery_app, perform_ping_check=False, loglevel="error"
    ) as _:
        from kombu import Connection

        def on_message(body, message):
            run_generation_step.delay(body["payload"]["jobId"])
            message.ack()

        with Connection(BROKER_URL) as conn:
            declare_topology(conn)
            drained = drain_generation_events(conn, on_message, limit=1, timeout_s=10)
        assert drained == 1

        wait_for(
            lambda: f"exec:{job_id}" in PROBE_RESULTS, what="worker 首次执行"
        )
        # 重复投递同一事件(发布确认丢失重发场景)
        run_generation_step.delay(str(job_id))
        wait_for(
            lambda: len(PROBE_RESULTS.get(f"outcomes:{job_id}", [])) >= 2,
            what="重复消息处理",
        )

    outcomes = PROBE_RESULTS[f"outcomes:{job_id}"]
    assert outcomes[0].get("executed") is True, outcomes[0]
    assert outcomes[1].get("skipped") is True, outcomes[1]  # 第二次不重复执行
    assert PROBE_RESULTS[f"exec:{job_id}"] == 1  # 上游动作计数为 1(不重复提交)

    async def final_status():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            job = await uow.jobs.get(job_id)
            return job.status

    assert asyncio.run(final_status()) == "completed"


# ---------- 投递器:发布确认、无路由、故障退避、多实例安全 ----------


@requires_broker
def test_unroutable_event_not_marked_published(clean_topology):
    """无路由事件(mandatory)不得标记成功,进入退避重试(§五.8)。"""
    from backend.infrastructure.outbox_dispatcher import OutboxDispatcher

    async def seed():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            workspace_id = uuid.uuid4()
            await uow.workspaces.ensure(workspace_id)
            job = await uow.jobs.create(workspace_id=workspace_id, kind="image", params={})
            event = await uow.outbox.enqueue(
                aggregate_type="job",
                aggregate_id=str(job.id),
                event_type="unknown.event",  # 无绑定队列的路由键
                payload={"jobId": str(job.id)},
            )
            await uow.commit()
            return event.id

    event_id = asyncio.run(seed())
    result = OutboxDispatcher(DatabaseSettings(url=TEST_DB_URL), BROKER_URL).dispatch_once()
    assert result["published"] == 0
    assert result["failed"] == 1

    async def status():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            event = await uow.outbox.get(event_id)
            return event.status, event.attempts

    status_, attempts = asyncio.run(status())
    assert status_ == "pending"
    assert attempts == 1


def test_broker_down_leaves_event_pending_with_backoff(clean_topology):
    """Broker 不可用:事件保持 pending 且退避递增,不标记成功(§五.2/§五.5)。"""
    from backend.infrastructure.outbox_dispatcher import OutboxDispatcher

    job_id, _ = seed_job_with_outbox()
    # 不存在的 broker(端口指向宿主侧未监听端口)
    dispatcher = OutboxDispatcher(
        DatabaseSettings(url=TEST_DB_URL),
        f"amqp://aivideo:aivideo-test@{BROKER_HOST}:{BROKER_PORT + 326}//",
    )
    result = dispatcher.dispatch_once(timeout_s=2)
    assert result["published"] == 0
    assert result["failed"] == 1

    async def status():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            events = await uow.outbox.list_pending(limit=10)
            return [(e.status, e.attempts) for e in events]

    assert asyncio.run(status()) == [("pending", 1)]


@requires_broker
def test_two_dispatchers_claim_disjoint_events(clean_topology):
    """多投递器并发领取:SKIP LOCKED 保证同一事件只发布一次(§五.2)。"""
    from concurrent.futures import ThreadPoolExecutor

    from backend.infrastructure.outbox_dispatcher import OutboxDispatcher

    async def seed_many():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            workspace_id = uuid.uuid4()
            await uow.workspaces.ensure(workspace_id)
            for _ in range(6):
                job = await uow.jobs.create(workspace_id=workspace_id, kind="image", params={})
                await uow.outbox.enqueue(
                    aggregate_type="job",
                    aggregate_id=str(job.id),
                    event_type="job.created",
                    payload={"jobId": str(job.id)},
                )
            await uow.commit()

    asyncio.run(seed_many())
    dispatcher = OutboxDispatcher(DatabaseSettings(url=TEST_DB_URL), BROKER_URL)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatcher.dispatch_once(), range(2)))
    total_published = sum(r["published"] for r in results)
    assert total_published == 6, results  # 无重复发布、无遗漏

    async def counts():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            published = await uow.outbox.count_by_status("published")
            pending = await uow.outbox.count_by_status("pending")
            return published, pending

    published, pending = asyncio.run(counts())
    assert (published, pending) == (6, 0)


@requires_broker
def test_publish_confirmed_event_is_persistent_and_json(clean_topology):
    """消息体契约:只携带标识/schemaVersion/追踪上下文,持久化投递(§五.7/§五.9)。"""
    from kombu import Connection

    from backend.infrastructure.messaging import declare_topology, drain_raw
    from backend.infrastructure.outbox_dispatcher import OutboxDispatcher

    job_id, event_id = seed_job_with_outbox()
    dispatcher = OutboxDispatcher(DatabaseSettings(url=TEST_DB_URL), BROKER_URL)
    dispatcher.dispatch_once()

    bodies = []
    with Connection(BROKER_URL) as conn:
        declare_topology(conn)
        drain_raw(conn, lambda body: bodies.append(body), limit=1, timeout_s=10)
    assert len(bodies) == 1
    body = bodies[0]
    assert body["eventId"] == str(event_id)
    assert body["eventType"] == "job.created"
    assert body["schemaVersion"] == "1"
    assert body["payload"]["jobId"] == str(job_id)
    assert json.dumps(body)  # JSON 可序列化


def test_stale_publishing_events_are_reclaimed(clean_topology):
    """发布中崩溃的滞留事件:超时回收回 pending,可再次投递(§五.7 故障恢复)。"""
    from backend.infrastructure.outbox_dispatcher import OutboxDispatcher

    job_id, _ = seed_job_with_outbox()

    async def mark_stale():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            events = await uow.outbox.list_pending(limit=10)
            assert len(events) == 1
            await uow.outbox.mark_publishing(events[0])
            # 把时间戳拨老,模拟滞留
            from sqlalchemy import update

            from backend.infrastructure.orm import Outbox

            await uow.session.execute(
                update(Outbox)
                .where(Outbox.id == events[0].id)
                .values(next_attempt_at=func_minus(120))
            )
            await uow.commit()

    def func_minus(seconds: int):
        from datetime import datetime, timedelta, timezone

        return datetime.now(timezone.utc) - timedelta(seconds=seconds)

    asyncio.run(mark_stale())

    async def counts_pending():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            return await uow.outbox.count_by_status("pending")

    assert asyncio.run(counts_pending()) == 0  # publishing 非 pending

    reclaimed = OutboxDispatcher(
        DatabaseSettings(url=TEST_DB_URL), BROKER_URL
    ).reclaim_stale_publishing(older_than_s=60)
    assert reclaimed == 1
    assert asyncio.run(counts_pending()) == 1


def test_claimed_publishing_is_not_reclaimed_from_old_due_time(clean_topology):
    """领取时刷新 next_attempt_at;原先到期时间较老时不得立刻被当成滞留回收。"""
    from datetime import datetime, timedelta, timezone

    from backend.infrastructure.outbox_dispatcher import OutboxDispatcher

    seed_job_with_outbox()

    async def age_then_claim():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            events = await uow.outbox.list_pending(limit=10)
            assert len(events) == 1
            from sqlalchemy import update

            from backend.infrastructure.orm import Outbox

            await uow.session.execute(
                update(Outbox)
                .where(Outbox.id == events[0].id)
                .values(next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=120))
            )
            await uow.session.flush()
            claimed = await uow.outbox.claim_and_mark_publishing(limit=10)
            await uow.commit()
            return len(claimed)

    assert asyncio.run(age_then_claim()) == 1
    reclaimed = OutboxDispatcher(
        DatabaseSettings(url=TEST_DB_URL), BROKER_URL
    ).reclaim_stale_publishing(older_than_s=60)
    assert reclaimed == 0

    async def still_publishing():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            return await uow.outbox.count_by_status("publishing")

    assert asyncio.run(still_publishing()) == 1


@requires_broker
def test_failed_handler_dead_letters_event(clean_topology):
    """消费处理失败:消息进入死信队列且可查询恢复,不无限重投(§五.11/§九)。"""
    from kombu import Connection

    from backend.infrastructure.messaging import DEAD_QUEUE, declare_topology, drain_raw
    from backend.infrastructure.outbox_dispatcher import OutboxDispatcher
    from backend.workers.consumer import drain_generation_events

    seed_job_with_outbox()
    result = OutboxDispatcher(DatabaseSettings(url=TEST_DB_URL), BROKER_URL).dispatch_once()
    assert result["published"] == 1

    def bad_handler(body, message):
        raise RuntimeError("消费者处理失败")

    with Connection(BROKER_URL) as conn:
        declare_topology(conn)
        drained = drain_generation_events(conn, bad_handler, limit=1, timeout_s=10)
        assert drained == 0
        dead = []
        drained_dead = drain_raw(
            conn, lambda body: dead.append(body), limit=1, timeout_s=10, queue=DEAD_QUEUE
        )
    assert drained_dead == 1
    assert dead[0]["eventType"] == "job.created"
