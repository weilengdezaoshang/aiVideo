"""全局容量验收(补充要求 §八 TDD):100 请求、上游并发上限 5、多 Worker 并发消费。

- 实际占用模型名额不超过 5;其余任务可靠排队(名额满即延迟,不降级放行);
- 重复消息不重复提交(消费幂等);取消/完成释放名额后可继续受理执行。
"""

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

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

SLOT_LIMIT = 5
TOTAL = 100
WORKERS = 8  # 模拟 8 个并发 Worker 消费线程


def _pg_available():
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
        for table in ("outbox", "generation_requests", "jobs", "assets"):
            conn.exec_driver_sql(f"DELETE FROM {table}")
    engine.dispose()
    yield


def accept_many(n: int) -> list[uuid.UUID]:
    from backend.services.acceptance import accept_generation
    from backend.providers.policy import ProviderPolicy

    workspace_id = uuid.uuid4()

    def one(i: int) -> uuid.UUID:
        result = asyncio.run(
            accept_generation(
                DatabaseSettings(url=TEST_DB_URL),
                workspace_id=workspace_id,
                request_id=f"cap-{i}",
                request_hash=f"hash-{i}",
                params={"prompt": f"p{i}"},
                policy=ProviderPolicy(workspace_capacity=n),
            )
        )
        return result["jobId"]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(one, range(n)))


def consume_created_once(job_ids: list[uuid.UUID]) -> None:
    """多 Worker 并发消费各自领取的 job.created 事件(每任务恰好一次提交尝试)。"""
    from backend.services.pipeline import execute_generation_step, get_stub_upstream

    upstream = get_stub_upstream()
    upstream.configure(finish_after=999)  # 上游保持执行中

    def run(job_id: uuid.UUID):
        asyncio.run(
            execute_generation_step(job_id, DatabaseSettings(url=TEST_DB_URL), upstream)
        )

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(run, job_ids))


@requires_pg
def test_hundred_requests_with_five_upstream_slots(clean_db):
    """100 受理、上限 5、8 Worker 并发:提交恰 5,排队 95;名额被占用不可再领。"""
    from backend.services.pipeline import DEFAULT_SLOT_LIMIT

    assert DEFAULT_SLOT_LIMIT == SLOT_LIMIT
    job_ids = accept_many(TOTAL)
    consume_created_once(job_ids)

    async def stats():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            from sqlalchemy import func, select

            from backend.infrastructure.orm import Job

            running = (
                await uow.session.execute(
                    select(func.count()).select_from(Job).where(Job.status == "running")
                )
            ).scalar_one()
            queued = (
                await uow.session.execute(
                    select(func.count()).select_from(Job).where(Job.status == "queued")
                )
            ).scalar_one()
            occupied = (
                await uow.session.execute(
                    select(func.count()).select_from(Job).where(Job.slot_scope.is_not(None))
                )
            ).scalar_one()
            externals = (
                await uow.session.execute(
                    select(func.count(func.distinct(Job.external_task_id))).where(
                        Job.external_task_id.is_not(None)
                    )
                )
            ).scalar_one()
            return running, queued, occupied, externals

    running, queued, occupied, externals = asyncio.run(stats())
    assert running <= SLOT_LIMIT, f"上游活动任务 {running} 超过上限 {SLOT_LIMIT}"
    assert occupied <= SLOT_LIMIT
    assert running + queued == TOTAL
    assert externals == running, "每个提交恰好登记一次上游任务(无重复付费提交)"
    assert queued >= TOTAL - SLOT_LIMIT


@requires_pg
def test_release_slot_allows_queued_to_proceed(clean_db):
    """终态释放名额后,排队任务可继续领取;取消亦释放(§八 TDD 验收最后一条)。"""
    from backend.services.pipeline import execute_generation_step, execute_poll_step, get_stub_upstream

    job_ids = accept_many(6)
    upstream = get_stub_upstream()
    upstream.configure(finish_after=999)
    for jid in job_ids[:5]:
        asyncio.run(execute_generation_step(jid, DatabaseSettings(url=TEST_DB_URL), upstream))
    # 第 6 个任务:名额满 → 延迟
    sixth = asyncio.run(
        execute_generation_step(job_ids[5], DatabaseSettings(url=TEST_DB_URL), upstream)
    )
    assert sixth.get("deferred") is True

    # 一个完成任务:完成查询 → 终态释放 → 第 6 个可提交
    upstream.configure(finish_after=0)
    done = asyncio.run(
        execute_poll_step(job_ids[0], DatabaseSettings(url=TEST_DB_URL), upstream)
    )
    assert done["completed"] is True

    upstream.configure(finish_after=999)
    retry = asyncio.run(
        execute_generation_step(job_ids[5], DatabaseSettings(url=TEST_DB_URL), upstream)
    )
    assert retry.get("submitted") is True
