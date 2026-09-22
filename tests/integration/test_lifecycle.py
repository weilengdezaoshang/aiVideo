"""分阶段执行生命周期(补充要求 §五.1/§七/§八):受理→提交→到期查询→终态。

前置:同 test_delivery_loop.py 的容器编排;aivideo-test 网络内执行。
"""

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

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

from backend.workers.probe import PROBE_RESULTS  # noqa: E402


def _pg_available() -> bool:
    import psycopg

    try:
        with psycopg.connect(PG_URL, connect_timeout=3):
            return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL 隔离实例不可用,集成测试未执行(不视为通过)",
)


@pytest.fixture()
def clean_db():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
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
    PROBE_RESULTS.clear()
    yield


def wait_for(predicate, timeout=30.0, what=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"等待超时:{what}")


async def get_job(job_id):
    async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
        job = await uow.jobs.get(job_id)
        return dict(
            status=job.status,
            external_task_id=job.external_task_id,
            next_poll_at=job.next_poll_at,
            attempt_count=job.attempt_count,
            slot_scope=job.slot_scope,
            error_code=job.error_code,
            recovery=job.recovery,
        )


# ---------- 受理(§五.1):任务+幂等+Outbox 同事务 ----------


@requires_pg
def test_accept_persists_job_request_outbox_atomically(clean_db):
    """受理同事务持久化任务/幂等记录/Outbox;幂等重放返回同一任务;同键不同参数冲突。"""
    from backend.errors import ConflictError
    from backend.services.acceptance import accept_generation

    workspace_id = uuid.uuid4()

    async def accept(request_hash, prompt):
        return await accept_generation(
            DatabaseSettings(url=TEST_DB_URL),
            workspace_id=workspace_id,
            request_id="req-1",
            request_hash=request_hash,
            params={"prompt": prompt},
        )

    first = asyncio.run(accept("h1", "猫"))
    assert first["replayed"] is False

    async def counts():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            return (
                await uow.outbox.count_by_status("pending"),
                len(await uow.jobs.list_by_workspace(workspace_id, limit=10)),
            )

    assert asyncio.run(counts()) == (1, 1)  # 同事务提交:一个任务、一条待投递事件

    replay = asyncio.run(accept("h1", "猫"))
    assert replay["replayed"] is True and replay["jobId"] == first["jobId"]

    with pytest.raises(ConflictError):
        asyncio.run(accept("h2-different", "狗"))


# ---------- 提交阶段(§七.1):领取名额→提交→登记上游→安排查询 ----------


@requires_pg
def test_submit_phase_acquires_slot_and_schedules_poll(clean_db):
    """提交阶段:领取模型名额,写入上游任务 ID 与下次查询时间;名额在执行期间保持占用。"""
    from backend.services.pipeline import execute_generation_step, get_stub_upstream

    async def seed():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            workspace_id = uuid.uuid4()
            await uow.workspaces.ensure(workspace_id)
            job = await uow.jobs.create(workspace_id=workspace_id, kind="image", params={})
            await uow.commit()
            return job.id

    job_id = asyncio.run(seed())
    outcome = asyncio.run(
        execute_generation_step(job_id, DatabaseSettings(url=TEST_DB_URL), get_stub_upstream())
    )
    assert outcome["submitted"] is True
    state = asyncio.run(get_job(job_id))
    assert state["status"] == "running"
    assert state["external_task_id"], "提交后必须登记上游任务 ID(§六/原任务已知问题 #3)"
    assert state["next_poll_at"] is not None
    assert state["slot_scope"], "上游生成期间模型名额必须被占用(§八.2)"


# ---------- 到期查询(§七):调度领取→查询→退避/终态 ----------


@requires_pg
def test_scheduler_claims_due_polls_and_advances_or_completes(clean_db):
    """到期调度:领取 running+到期任务并派发 poll_due;未完成→退避推进;完成→终态+资产。"""

    from sqlalchemy import create_engine

    from backend.services.pipeline import (
        execute_poll_step,
        get_stub_upstream,
        scan_due_polls,
    )
    from backend.services.pipeline import PollPolicy

    async def seed_submitted(unfinished: int):
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            workspace_id = uuid.uuid4()
            await uow.workspaces.ensure(workspace_id)
            job = await uow.jobs.create(workspace_id=workspace_id, kind="image", params={})
            job_id = job.id
            assert await uow.jobs.transition(job_id, {"queued"}, "running") is True
            await uow.jobs.set_poll_schedule(
                job_id,
                external_task_id=f"stub-{str(job_id)[:8]}",
                next_poll_at=datetime.now(timezone.utc) - timedelta(seconds=1),  # 已到期
            )
            await uow.jobs.try_acquire_slot(job_id, scope="stub-model", limit=5)
            await uow.commit()
            return job_id

    upstream = get_stub_upstream()
    upstream.configure(finish_after=1)  # 第一次查询未完成,第二次完成

    job_id = asyncio.run(seed_submitted(1))
    claimed = scan_due_polls(DatabaseSettings(url=TEST_DB_URL))
    assert claimed == 1, "调度器应恰好领取一条到期任务"

    async def pending_events():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            events = await uow.outbox.list_pending(limit=10)
            return [e.event_type for e in events]

    assert asyncio.run(pending_events()) == ["job.poll_due"]  # 调度派发一条查询事件

    policy = PollPolicy(base_backoff_s=1.0, max_backoff_s=8.0, deadline_s=600.0)
    outcome = asyncio.run(
        execute_poll_step(job_id, DatabaseSettings(url=TEST_DB_URL), upstream, policy)
    )
    assert outcome["rescheduled"] is True
    state = asyncio.run(get_job(job_id))
    assert state["status"] == "running"
    assert state["next_poll_at"] > datetime.now(timezone.utc), "退避应推进到未来"
    assert state["attempt_count"] == 1

    # 拨时间到期,再次调度+查询 → 完成
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE jobs SET next_poll_at = now() - interval '1 second' WHERE id = '%s'" % job_id
        )
    engine.dispose()
    assert scan_due_polls(DatabaseSettings(url=TEST_DB_URL)) == 1
    outcome = asyncio.run(
        execute_poll_step(job_id, DatabaseSettings(url=TEST_DB_URL), upstream, policy)
    )
    assert outcome["completed"] is True
    state = asyncio.run(get_job(job_id))
    assert state["status"] == "completed"
    assert state["slot_scope"] is None, "终态必须释放模型名额(§八.2)"

    async def asset_count():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            return await uow.assets.count()

    assert asyncio.run(asset_count()) == 1  # 成功产物登记为资产


@requires_pg
def test_due_poll_claim_is_not_reissued_until_rescheduled(clean_db):
    """领取到期任务后必须清空 next_poll_at,否则 beat 会每 5 秒重复入队。"""
    from backend.services.pipeline import scan_due_polls

    async def seed_due():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            workspace_id = uuid.uuid4()
            await uow.workspaces.ensure(workspace_id)
            job = await uow.jobs.create(workspace_id=workspace_id, kind="image", params={})
            await uow.jobs.transition(job.id, {"queued"}, "running")
            await uow.jobs.set_poll_schedule(
                job.id,
                external_task_id="stub-lease",
                next_poll_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
            await uow.commit()
            return job.id

    job_id = asyncio.run(seed_due())
    settings = DatabaseSettings(url=TEST_DB_URL)
    assert scan_due_polls(settings) == 1
    assert scan_due_polls(settings) == 0
    state = asyncio.run(get_job(job_id))
    assert state["next_poll_at"] is None


@requires_pg
def test_poll_deadline_transitions_to_reconcilable_unknown(clean_db):
    """查询截止时间已过:任务进入可对账的 unknown,名额释放,不自动重提(§七/原已知问题 #3)。"""


    from backend.services.pipeline import PollPolicy, execute_poll_step, get_stub_upstream

    async def seed():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            workspace_id = uuid.uuid4()
            await uow.workspaces.ensure(workspace_id)
            job = await uow.jobs.create(workspace_id=workspace_id, kind="video", params={})
            job_id = job.id
            await uow.jobs.transition(job_id, {"queued"}, "running")
            await uow.jobs.set_poll_schedule(
                job_id, external_task_id="stub-deadline", next_poll_at=datetime.now(timezone.utc)
            )
            await uow.jobs.try_acquire_slot(job_id, scope="stub-model", limit=5)
            # 受理时间拨到 1 小时前:超过 deadline
            from backend.infrastructure.orm import Job

            await uow.session.execute(
                Job.__table__.update()
                .where(Job.__table__.c.id == job_id)
                .values(created_at=datetime.now(timezone.utc) - timedelta(hours=1))
            )
            await uow.commit()
            return job_id

    job_id = asyncio.run(seed())
    outcome = asyncio.run(
        execute_poll_step(
            job_id,
            DatabaseSettings(url=TEST_DB_URL),
            get_stub_upstream(),
            PollPolicy(base_backoff_s=1.0, max_backoff_s=8.0, deadline_s=60.0),
        )
    )
    assert outcome["unknown"] is True
    state = asyncio.run(get_job(job_id))
    assert state["status"] == "unknown"
    assert state["error_code"] == "UPSTREAM_UNKNOWN"
    assert state["recovery"] == "reconcile"
    assert state["slot_scope"] is None


# ---------- 模型名额并发原语(§八.2/§八.6):原子领取、按所有者释放 ----------


@requires_pg
def test_model_slot_atomic_acquire_under_concurrency(clean_db):
    """并发领取模型名额:无论多少并发,占用数不超过上限;释放后可再领。"""
    from concurrent.futures import ThreadPoolExecutor

    from backend.services.pipeline import (
        execute_generation_step,
        execute_poll_step,
        get_stub_upstream,
    )

    async def seed_many(n):
        ids = []
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            workspace_id = uuid.uuid4()
            await uow.workspaces.ensure(workspace_id)
            for _ in range(n):
                job = await uow.jobs.create(workspace_id=workspace_id, kind="image", params={})
                ids.append(job.id)
            await uow.commit()
        return ids

    job_ids = asyncio.run(seed_many(20))
    upstream = get_stub_upstream()
    upstream.configure(finish_after=999)  # 保持上游执行中,名额不释放

    def submit(job_id):
        return asyncio.run(
            execute_generation_step(job_id, DatabaseSettings(url=TEST_DB_URL), upstream)
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(submit, job_ids))

    submitted = [o for o in outcomes if o.get("submitted")]
    deferred = [o for o in outcomes if o.get("deferred")]
    assert len(submitted) == 5, f"并发上限 5,实际提交 {len(submitted)}"
    assert len(deferred) == 15
    assert all(o["reason"] == "slot" for o in deferred)

    async def occupied():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            return await uow.jobs.count_occupied_slots("stub-model")

    assert asyncio.run(occupied()) == 5  # 数据库即全局协调依据

    # 释放一个名额后,延迟任务可再次提交
    submitted_ids = [jid for jid, o in zip(job_ids, outcomes) if o.get("submitted")]
    released = submitted_ids[0]
    upstream.configure(finish_after=0)
    asyncio.run(execute_poll_step(released, DatabaseSettings(url=TEST_DB_URL), upstream))
    assert asyncio.run(occupied()) == 4
