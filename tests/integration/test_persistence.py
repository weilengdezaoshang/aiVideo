"""ORM、事务与连接生命周期(补充要求 §三):真实 PostgreSQL 隔离实例验证。

前置:docker run -d --name aivideo-test-pg -e POSTGRES_USER=aivideo \
  -e POSTGRES_PASSWORD=aivideo-test -e POSTGRES_DB=aivideo_test -p 55432:5432 postgres:16-alpine
基础设施不可用时本文件显式 skip(未执行),不冒充通过。
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.errors import ConflictError
from backend.infrastructure.database import DatabaseSettings
from backend.infrastructure.uow import AsyncUnitOfWork

# 隔离实例地址可被环境覆盖(容器化执行时指向容器名)。
TEST_URL = os.environ.get(
    "AIVERO_TEST_DB_URL",
    "postgresql+psycopg://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test",
)


def _pg_available(url: str) -> bool:
    import psycopg

    try:
        with psycopg.connect(url.replace("+psycopg", ""), connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(TEST_URL.replace("+psycopg", "")),
    reason="PostgreSQL 隔离实例不可用,集成测试未执行(不视为通过)",
)

TABLES = [
    "asset_references",
    "assets",
    "attempts",
    "outbox",
    "generation_requests",
    "jobs",
    "documents",
    "workspaces",
]


@pytest.fixture()
def settings():
    return DatabaseSettings(url=TEST_URL, pool_size=5, max_overflow=2)


@pytest.fixture()
def upgraded(settings):
    """每个用例:重建 Schema(alembic head)+ 用例后清空数据。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    _truncate_sync()


def _truncate_sync():
    from sqlalchemy import create_engine

    engine = create_engine(TEST_URL)
    with engine.begin() as conn:
        for table in TABLES:
            conn.exec_driver_sql(f"DELETE FROM {table}")
    engine.dispose()


@pytest.fixture()
def workspace_id():
    return uuid.uuid4()


def test_alembic_upgrade_creates_expected_schema(upgraded, settings):
    """迁移 0001 建立 8 张表与关键索引,模型与库无漂移。"""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    from backend.infrastructure.orm import Base

    sync_engine = create_engine(TEST_URL)
    context = MigrationContext.configure(sync_engine.connect())
    diff = compare_metadata(context, Base.metadata)
    sync_engine.dispose()
    assert diff == [], f"模型与数据库存在漂移: {diff}"

    async def check_index():
        engine = create_async_engine(TEST_URL)
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT 1 FROM pg_indexes WHERE indexname = 'ix_jobs_status_next_poll'"
                )
            )
            assert rows.scalar() == 1
        await engine.dispose()

    asyncio.run(check_index())


def test_uow_commit_persists_request_job_outbox_atomically(upgraded, settings, workspace_id):
    """任务、幂等记录与 Outbox 在同一事务提交:全部可见或全部不可见。"""

    async def scenario(persist: bool):
        engine = create_async_engine(TEST_URL)
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_URL, pool_size=5, max_overflow=2)) as uow:
            await uow.workspaces.ensure(workspace_id)
            job = await uow.jobs.create(
                workspace_id=workspace_id, kind="image", params={"prompt": "猫"}
            )
            await uow.requests.register(
                workspace_id=workspace_id,
                request_id="idem-1",
                request_hash="hash-1",
                params={"prompt": "猫"},
                job_id=job.id,
            )
            await uow.outbox.enqueue(
                aggregate_type="job",
                aggregate_id=str(job.id),
                event_type="job.created",
                payload={"jobId": str(job.id)},
            )
            if persist:
                await uow.commit()
            else:
                await uow.rollback()
        await engine.dispose()

    asyncio.run(scenario(persist=True))
    asyncio.run(scenario(persist=False))  # 第二次同键:回滚后首次数据仍在,同键注册应冲突

    async def counts():
        engine = create_async_engine(TEST_URL)
        async with engine.connect() as conn:
            jobs = (await conn.execute(text("SELECT count(*) FROM jobs"))).scalar()
            outbox = (await conn.execute(text("SELECT count(*) FROM outbox"))).scalar()
            requests = (
                await conn.execute(text("SELECT count(*) FROM generation_requests"))
            ).scalar()
        await engine.dispose()
        return jobs, outbox, requests

    jobs, outbox, requests = asyncio.run(counts())
    assert (jobs, outbox, requests) == (1, 1, 1)  # 回滚事务的痕迹不存在


def test_duplicate_request_key_conflicts_and_scope_isolated(upgraded, workspace_id):
    """同 workspace 幂等键唯一;同键不同参数返回冲突;跨 workspace 不受影响。"""

    async def scenario():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_URL, pool_size=5, max_overflow=2)) as uow:
            await uow.workspaces.ensure(workspace_id)
            job = await uow.jobs.create(workspace_id=workspace_id, kind="image", params={})
            await uow.requests.register(workspace_id, "idem-x", "h1", {"prompt": "a"}, job.id)
            await uow.commit()
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_URL, pool_size=5, max_overflow=2)) as uow:
            job2 = await uow.jobs.create(workspace_id=workspace_id, kind="image", params={})
            with pytest.raises(ConflictError):
                await uow.requests.register(
                    workspace_id, "idem-x", "h2-different-params", {"prompt": "b"}, job2.id
                )
            await uow.rollback()
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_URL, pool_size=5, max_overflow=2)) as uow:
            other_ws = uuid.uuid4()
            await uow.workspaces.ensure(other_ws)
            job3 = await uow.jobs.create(workspace_id=other_ws, kind="image", params={})
            await uow.requests.register(
                other_ws, "idem-x", "h1", {"prompt": "a"}, job3.id
            )
            await uow.commit()

    asyncio.run(scenario())


def test_job_transition_guarded_by_state_version(upgraded, workspace_id):
    """状态转换带版本条件:过期版本不得覆盖,终态不被迟到事件改写。"""

    async def scenario():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_URL, pool_size=5, max_overflow=2)) as uow:
            await uow.workspaces.ensure(workspace_id)
            job = await uow.jobs.create(workspace_id=workspace_id, kind="video", params={})
            await uow.commit()
            job_id = job.id
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_URL, pool_size=5, max_overflow=2)) as uow:
            assert await uow.jobs.transition(job_id, {"queued"}, "running") is True
            assert await uow.jobs.transition(job_id, {"running"}, "completed") is True
            await uow.commit()
        # 迟到事件(独立事务):以过期版本试图把终态改写为 failed,必须不生效。
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_URL, pool_size=5, max_overflow=2)) as uow:
            assert (
                await uow.jobs.transition(job_id, {"running"}, "failed", expected_version=1)
                is False
            )
            await uow.rollback()
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_URL, pool_size=5, max_overflow=2)) as uow:
            assert (await uow.jobs.get(job_id)).status == "completed"

    asyncio.run(scenario())


def test_job_pagination(upgraded, workspace_id):
    """任务列表分页:页大小与排序稳定。"""

    async def scenario():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_URL, pool_size=5, max_overflow=2)) as uow:
            await uow.workspaces.ensure(workspace_id)
            for _ in range(25):
                await uow.jobs.create(workspace_id=workspace_id, kind="image", params={})
            await uow.commit()
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_URL, pool_size=5, max_overflow=2)) as uow:
            page1 = await uow.jobs.list_by_workspace(workspace_id, limit=10, offset=0)
            page2 = await uow.jobs.list_by_workspace(workspace_id, limit=10, offset=10)
            page3 = await uow.jobs.list_by_workspace(workspace_id, limit=10, offset=20)
        assert [len(p) for p in (page1, page2, page3)] == [10, 10, 5]
        created = [j.created_at for j in page1]
        assert created == sorted(created, reverse=True)

    asyncio.run(scenario())
