"""PostgreSQL batch quota claim. Skips until the parent provides a migrated database."""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import pytest

from backend.core.batch.contracts import BatchSettings
from backend.core.batch.store import ack_dispatch, claim_dispatch, recover_reserved, settle_dispatch
from backend.core.contracts import RunInput
from backend.core.orm import CoreRun, CoreSession
from backend.core.store import create_run
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Workspace

DB = os.environ.get("AIVERO_CORE_TEST_DB") or os.environ.get("AIVERO_DURABLE_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated PostgreSQL required")


@asynccontextmanager
async def fixture():
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    factory = create_async_session_factory(engine)
    workspace = uuid.uuid4()
    from sqlalchemy import select
    from backend.core.batch.orm import CoreBatchQuota
    from backend.core.batch.store import ensure_quota
    async with factory.begin() as db:
        quota = await db.scalar(select(CoreBatchQuota).where(CoreBatchQuota.quota_key == "global"))
        prior_limit = quota.limit_value if quota is not None else BatchSettings().global_concurrency
        quota = await ensure_quota(db)
        assert quota.inflight == 0, "test database must not have active batch work"
        quota.limit_value = 1
    async with factory.begin() as db:
        db.add(Workspace(id=workspace))
        await db.flush()
        session = CoreSession(workspace_id=workspace, source_canvas_id=uuid.uuid4(), user_id="batch-user", language="zh-CN")
        db.add(session)
        await db.flush()
        run = await create_run(db, RunInput(
            session_id=session.id, workspace_id=workspace, user_id="batch-user", request_key=uuid.uuid4().hex,
            request_hash="ab" * 32, snapshot={}, scope={}, invariants={}, version_lock={}))
        run_id = run.id
    try:
        yield factory, workspace, run_id
    finally:
        async with factory.begin() as db:
            quota = await db.scalar(select(CoreBatchQuota).where(CoreBatchQuota.quota_key == "global").with_for_update())
            if quota is not None and quota.inflight == 0:
                quota.limit_value = prior_limit
        await engine.dispose()


def test_claim_is_idempotent_and_budget_rejection_does_not_queue():
    async def body():
        settings = BatchSettings(global_concurrency=1, max_per_run=1, max_per_batch=1)
        async with fixture() as (factory, workspace, run_id):
            async with factory.begin() as db:
                first = await claim_dispatch(db, workspace_id=workspace, run_id=run_id, task_key="t1",
                                             credits=2, budget_left=5, settings=settings)
                assert first == "reserved"
            async with factory.begin() as db:
                again = await claim_dispatch(db, workspace_id=workspace, run_id=run_id, task_key="t1",
                                             credits=2, budget_left=5, settings=settings)
                assert again == "duplicate:reserved"
                blocked = await claim_dispatch(db, workspace_id=workspace, run_id=run_id, task_key="t2",
                                               credits=1, budget_left=5, settings=settings)
                assert blocked == "concurrency_rejected"
                denied = await claim_dispatch(db, workspace_id=workspace, run_id=run_id, task_key="t3",
                                              credits=9, budget_left=1, settings=settings)
                assert denied == "budget_rejected"
                run = await db.get(CoreRun, run_id)
                assert run.status == "created"
            async with factory.begin() as db:
                assert await ack_dispatch(db, run_id, "t1") == "submitted"
                assert await ack_dispatch(db, run_id, "t1") == "duplicate:submitted"
                open_rows = await recover_reserved(db, workspace, 10)
                assert open_rows == []
                assert await settle_dispatch(db, run_id, "t1") == "settled"
                assert await settle_dispatch(db, run_id, "t1") == "duplicate:settled"
                assert await settle_dispatch(db, run_id, "missing") == "not_submitted"
    asyncio.run(body())


def test_terminal_settlement_defers_busy_global_quota_without_losing_slot():
    async def body():
        from sqlalchemy import select
        from backend.core.batch.orm import CoreBatchQuota
        settings = BatchSettings(global_concurrency=1, max_per_run=1, max_per_batch=1)
        async with fixture() as (factory, workspace, run_id):
            async with factory.begin() as tx:
                assert await claim_dispatch(tx, workspace_id=workspace, run_id=run_id,
                    task_key="t1", credits=1, budget_left=1, settings=settings) == "reserved"
                assert await ack_dispatch(tx, run_id, "t1") == "submitted"
            async with factory.begin() as holder:
                await holder.scalar(select(CoreBatchQuota).where(
                    CoreBatchQuota.quota_key == "global").with_for_update())
                async with factory.begin() as contender:
                    result = await asyncio.wait_for(settle_dispatch(contender, run_id, "t1"), timeout=1)
                    assert result == "quota_busy"
            async with factory.begin() as tx:
                assert await settle_dispatch(tx, run_id, "t1") == "settled"
                assert await settle_dispatch(tx, run_id, "t1") == "duplicate:settled"
                quota = await tx.scalar(select(CoreBatchQuota).where(CoreBatchQuota.quota_key == "global"))
                assert quota.inflight == 0
    asyncio.run(body())


def test_cancelled_run_terminal_task_slot_is_recovered_once():
    async def body():
        from sqlalchemy import select
        from backend.core.batch.orm import CoreBatchQuota
        from backend.core.batch.runtime import reconcile_terminal_dispatches
        from backend.core.store import transition
        from tests.integration.test_core_contracts import plan_and_task
        async with fixture() as (factory, workspace, run_id):
            async with factory.begin() as tx:
                task = await plan_and_task(tx, run_id)
                for target in ("validated", "queued", "cancelled"):
                    await transition(tx, "task", task.id, task.version, target)
                assert await claim_dispatch(tx, workspace_id=workspace, run_id=run_id,
                    task_key=task.task_key, credits=1, budget_left=1) == "reserved"
                run = await tx.get(CoreRun, run_id)
                await transition(tx, "run", run.id, run.version, "cancelled")
            assert await reconcile_terminal_dispatches(factory)
            assert not await reconcile_terminal_dispatches(factory)
            async with factory() as tx:
                quota = await tx.scalar(select(CoreBatchQuota).where(CoreBatchQuota.quota_key == "global"))
                assert quota.inflight == 0
    asyncio.run(body())
