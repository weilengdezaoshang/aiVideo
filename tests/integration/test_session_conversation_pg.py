"""Opt-in PostgreSQL tests for session intake, merge and submit fencing."""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.core.orm import CoreRun, CoreRunInputRevision, CoreSession, CoreSessionMessage, CoreTask
from backend.core.sessions.contracts import SessionSettings
from backend.core.sessions.dispatch import dispatch_due
from backend.core.sessions.fencing import ObsoleteRevision, assert_submittable, latch_first_submit
from backend.core.sessions.intake import enqueue_message
from backend.core.sessions.resolve import resolve_session
from backend.core.tools import build_catalog
from backend.core.tools.store import publish
from backend.infrastructure.database import (DatabaseSettings, create_async_database_engine,
    create_async_session_factory)
from backend.infrastructure.orm import Document, Workspace

DB = os.environ.get("AIVERO_CORE_TEST_DB") or os.environ.get("AIVERO_DURABLE_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated PostgreSQL required")


@asynccontextmanager
async def fixture():
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    factory = create_async_session_factory(engine)
    workspace = uuid.uuid4()
    canvas = uuid.uuid4()
    async with factory.begin() as tx:
        tx.add(Workspace(id=workspace, name="session-test"))
        await tx.flush()
        tx.add(Document(id=canvas, workspace_id=workspace, name="c", data={"objects": {}}, revision=1))
        ident = uuid.uuid4().hex
        catalog = build_catalog(Catalog(CatalogConfig(version=ident, environment="development",
            enabled=True, capabilities=[])), ident)
        await publish(tx, catalog, published_by="test")
    try:
        yield factory, workspace, canvas
    finally:
        from backend.core.supervisor.engine import stop
        from backend.core.supervisor.orm import Invocation
        from sqlalchemy import update
        async with factory.begin() as tx:
            owned = list(await tx.scalars(select(CoreRun).where(CoreRun.workspace_id == workspace)))
            for row in owned:
                if row.status not in {"completed", "failed", "cancelled"}:
                    await tx.scalar(select(CoreSession).where(CoreSession.id == row.session_id).with_for_update())
                    await stop(tx, row, "cancelled", "fixture cleanup", "fixture-cleanup:" + str(row.id))
            if owned:
                await tx.execute(update(Invocation).where(Invocation.run_id.in_([row.id for row in owned]))
                    .values(status="done", lease_token=None, lease_until=None))
        from sqlalchemy import func
        async with factory.begin() as tx:
            sessions = select(CoreSession.id).where(CoreSession.workspace_id == workspace)
            await tx.execute(update(CoreSessionMessage).where(CoreSessionMessage.session_id.in_(sessions),
                CoreSessionMessage.processing_status == "pending").values(processing_status="cancelled"))
            await tx.execute(update(CoreSession).where(CoreSession.workspace_id == workspace).values(
                closed_at=func.now(), dispatch_at=None, dispatch_lease_owner=None, dispatch_lease_until=None))
        await engine.dispose()


def test_idempotent_enqueue_and_hash_conflict():
    async def scenario():
      async with fixture() as (factory, workspace, canvas):
        async with factory.begin() as tx:
            session, _doc = await resolve_session(tx, workspace_id=workspace, user_id="u1", document_id=canvas)
            first = await enqueue_message(tx, session, client_id="c1", client_message_id="m1",
                content={"text": "狗"}, settings=SessionSettings(quiet_window_seconds=1.2))
            again = await enqueue_message(tx, session, client_id="c1", client_message_id="m1",
                content={"text": "狗"}, settings=SessionSettings())
            assert again.replayed is True
            assert again.message_id == first.message_id
            with pytest.raises(ValueError, match="request_key_conflict"):
                await enqueue_message(tx, session, client_id="c1", client_message_id="m1",
                    content={"text": "兔子"})
            other = await enqueue_message(tx, session, client_id="c2", client_message_id="m1",
                content={"text": "兔子"})
            assert other.replayed is False
            assert other.message_id != first.message_id
    asyncio.run(scenario())


def test_three_messages_merge_one_run():
    async def scenario():
      async with fixture() as (factory, workspace, canvas):
        settings = SessionSettings(quiet_window_seconds=0.01, max_merge_wait_seconds=4)
        async with factory.begin() as tx:
            session, _doc = await resolve_session(tx, workspace_id=workspace, user_id="u2", document_id=canvas)
            await enqueue_message(tx, session, client_id="c", client_message_id="a",
                content={"text": "狗", "budget": 10, "tokens": 1000, "rounds": 4}, settings=settings)
            await enqueue_message(tx, session, client_id="c", client_message_id="b",
                content={"text": "不对兔子"}, settings=settings)
            await enqueue_message(tx, session, client_id="c", client_message_id="c",
                content={"text": "再暗一点"}, settings=settings)
            session.dispatch_at = session.dispatch_at - timedelta(seconds=5)
            session_id = session.id
        assert await dispatch_due(factory, settings) is True
        async with factory() as tx:
            session = await tx.get(CoreSession, session_id)
            run = await tx.get(CoreRun, session.active_run_id)
            messages = list(await tx.scalars(select(CoreSessionMessage).where(
                CoreSessionMessage.session_id == session.id).order_by(CoreSessionMessage.sequence)))
            revision = await tx.scalar(select(CoreRunInputRevision).where(
                CoreRunInputRevision.run_id == run.id, CoreRunInputRevision.revision == 1))
            assert len(messages) == 3
            assert revision.effective_request["message"] == "狗 不对兔子 再暗一点"
            assert run.snapshot["request"]["message"] == "狗 不对兔子 再暗一点"
    asyncio.run(scenario())


def test_worker_rejects_obsolete_revision():
    async def scenario():
      async with fixture() as (factory, workspace, canvas):
        async with factory.begin() as tx:
            session, _doc = await resolve_session(tx, workspace_id=workspace, user_id="u3", document_id=canvas)
            from backend.core.contracts import RunInput, TaskInput, CoreSettings
            from backend.core.orm import CorePlan
            from backend.core.store import create_run, create_task
            run = await create_run(tx, RunInput(session_id=session.id, workspace_id=workspace, user_id="u3",
                request_key=uuid.uuid4().hex, request_hash="a" * 64, snapshot={"request": {"message": "x"}},
                scope={}, invariants={}, version_lock={}, input_revision=2), CoreSettings())
            plan = CorePlan(run_id=run.id, version=1, writer="rule", proposal={}, content_hash="b" * 64,
                rationale={}, link_constraints={}, result_type="generate", stage="execute", milestones=[],
                self_check={}, expansion_rules={}, version_lock={}, input_revision=1)
            tx.add(plan)
            await tx.flush()
            task = await create_task(tx, TaskInput(run_id=run.id, plan_id=plan.id, plan_version=1,
                task_key="t1", kind="call_tool", idempotency_key=uuid.uuid4().hex, parameters={},
                scope={}, input_revision=1))
            with pytest.raises(ObsoleteRevision):
                await assert_submittable(tx, run, task)
            task.input_revision = 2
            assert await assert_submittable(tx, run, task) is True
            await latch_first_submit(tx, run, task)
            assert run.first_submitted_task_id == task.id
    asyncio.run(scenario())


async def _seed_run_task(tx, session, workspace, user_id, task_revision=1):
    from backend.core.contracts import RunInput, TaskInput, CoreSettings
    from backend.core.orm import CorePlan
    from backend.core.store import create_run, create_task
    run = await create_run(tx, RunInput(session_id=session.id, workspace_id=workspace, user_id=user_id,
        request_key=uuid.uuid4().hex, request_hash="a" * 64, snapshot={"request": {"message": "x"}},
        scope={}, invariants={}, version_lock={}, input_revision=1), CoreSettings())
    session.active_run_id = run.id
    plan = CorePlan(run_id=run.id, version=1, writer="rule", proposal={}, content_hash="b" * 64,
        rationale={}, link_constraints={}, result_type="generate", stage="execute", milestones=[],
        self_check={}, expansion_rules={}, version_lock={}, input_revision=1)
    tx.add(plan)
    await tx.flush()
    task = await create_task(tx, TaskInput(run_id=run.id, plan_id=plan.id, plan_version=1,
        task_key="t1", kind="call_tool", idempotency_key=uuid.uuid4().hex, parameters={},
        scope={}, input_revision=task_revision))
    return run, plan, task


def test_pending_message_blocks_submit_when_fence_missed():
    async def scenario():
      async with fixture() as (factory, workspace, canvas):
        async with factory.begin() as tx:
            session, _doc = await resolve_session(tx, workspace_id=workspace, user_id="fence", document_id=canvas)
            run, _plan, task = await _seed_run_task(tx, session, workspace, "fence")
            run.submit_fence = 1
            tx.add(CoreSessionMessage(session_id=session.id, sequence=1, request_key="pending",
                role="user", content={"text": "兔子"}, processing_status="pending"))
            session.message_cursor = 1
            await tx.flush()
            with pytest.raises(ObsoleteRevision):
                await assert_submittable(tx, run, task)
    asyncio.run(scenario())


def test_intake_before_worker_fence():
    async def scenario():
      async with fixture() as (factory, workspace, canvas):
        async with factory.begin() as tx:
            session, _doc = await resolve_session(tx, workspace_id=workspace, user_id="race-in", document_id=canvas)
            run, _plan, task = await _seed_run_task(tx, session, workspace, "race-in")
            session_id, run_id, task_id = session.id, run.id, task.id
        async with factory.begin() as tx:
            session = await tx.scalar(select(CoreSession).where(CoreSession.id == session_id).with_for_update())
            await enqueue_message(tx, session, client_id="c", client_message_id="before",
                content={"text": "暗兔子"}, settings=SessionSettings(quiet_window_seconds=30))
        async with factory.begin() as tx:
            run = await tx.scalar(select(CoreRun).where(CoreRun.id == run_id).with_for_update())
            task = await tx.get(CoreTask, task_id)
            with pytest.raises(ObsoleteRevision):
                await assert_submittable(tx, run, task)
    asyncio.run(scenario())


def test_worker_before_intake_pending_still_blocks():
    async def scenario():
      async with fixture() as (factory, workspace, canvas):
        async with factory.begin() as tx:
            session, _doc = await resolve_session(tx, workspace_id=workspace, user_id="race-w", document_id=canvas)
            run, _plan, task = await _seed_run_task(tx, session, workspace, "race-w")
            session_id, run_id, task_id = session.id, run.id, task.id
            fence = int(run.submit_fence or 1)
        worker = factory()
        await worker.__aenter__()
        try:
            await worker.begin()
            run = await worker.scalar(select(CoreRun).where(CoreRun.id == run_id).with_for_update())
            task = await worker.get(CoreTask, task_id)
            async with factory.begin() as intake:
                session = await intake.scalar(select(CoreSession).where(CoreSession.id == session_id).with_for_update())
                await enqueue_message(intake, session, client_id="c", client_message_id="after",
                    content={"text": "再暗"}, settings=SessionSettings(quiet_window_seconds=30))
                bumped = await intake.get(CoreRun, run_id)
                assert int(bumped.submit_fence or 1) == fence
            with pytest.raises(ObsoleteRevision):
                await assert_submittable(worker, run, task)
            await worker.rollback()
        finally:
            await worker.__aexit__(None, None, None)
    asyncio.run(scenario())


def test_concurrent_first_session_opens_consistent_seq():
    async def scenario():
      async with fixture() as (factory, workspace, canvas):
        async def open_one(user, client, mid):
            async with factory.begin() as tx:
                session, _doc = await resolve_session(tx, workspace_id=workspace, user_id=user, document_id=canvas)
                result = await enqueue_message(tx, session, client_id=client, client_message_id=mid,
                    content={"text": "你好"}, settings=SessionSettings())
                return session.id, result.sequence
        left, right = await asyncio.gather(open_one("alice", "d1", "m1"), open_one("bob", "d2", "m1"))
        assert left[0] != right[0]
        same = await asyncio.gather(open_one("carol", "phone", "a"), open_one("carol", "tablet", "b"))
        assert same[0][0] == same[1][0]
        assert {same[0][1], same[1][1]} == {1, 2}
    asyncio.run(scenario())


def test_locked_session_does_not_block_other_dispatcher():
    async def scenario():
      async with fixture() as (factory, workspace, canvas):
        settings = SessionSettings(quiet_window_seconds=0.01)
        async with factory.begin() as tx:
            first, _ = await resolve_session(tx, workspace_id=workspace, user_id="d1", document_id=canvas)
            second, _ = await resolve_session(tx, workspace_id=workspace, user_id="d2", document_id=canvas)
            await enqueue_message(tx, first, client_id="c", client_message_id="a",
                content={"text": "一", "budget": 10, "tokens": 100, "rounds": 2}, settings=settings)
            await enqueue_message(tx, second, client_id="c", client_message_id="b",
                content={"text": "二", "budget": 10, "tokens": 100, "rounds": 2}, settings=settings)
            first.dispatch_at = first.dispatch_at - timedelta(seconds=5)
            second.dispatch_at = second.dispatch_at - timedelta(seconds=5)
            held_id, other_id = first.id, second.id
        holder = factory()
        await holder.__aenter__()
        try:
            await holder.begin()
            locked = await holder.scalar(select(CoreSession).where(CoreSession.id == held_id).with_for_update())
            assert locked is not None
            assert await dispatch_due(factory, settings) is True
            async with factory() as tx:
                other = await tx.get(CoreSession, other_id)
                held = await tx.get(CoreSession, held_id)
                assert other.active_run_id is not None
                assert held.active_run_id is None
            await holder.rollback()
        finally:
            await holder.__aexit__(None, None, None)
    asyncio.run(scenario())


def test_correction_acl_same_session_only():
    async def scenario():
      async with fixture() as (factory, workspace, canvas):
        async with factory.begin() as tx:
            owner, _ = await resolve_session(tx, workspace_id=workspace, user_id="owner", document_id=canvas)
            other, _ = await resolve_session(tx, workspace_id=workspace, user_id="intruder", document_id=canvas)
            run, _plan, _task = await _seed_run_task(tx, owner, workspace, "owner")
            with pytest.raises(ValueError, match="invalid_related_run"):
                await enqueue_message(tx, other, client_id="x", client_message_id="bad",
                    content={"text": "不对兔子"}, related_run_id=run.id)
            ok = await enqueue_message(tx, owner, client_id="x", client_message_id="ok",
                content={"text": "不对兔子"}, related_run_id=run.id)
            row = await tx.get(CoreSessionMessage, ok.message_id)
            assert row.correction_of_run_id == run.id
            assert (row.content or {}).get("feedback") is True
    asyncio.run(scenario())


def test_intake_emits_core_changed_not_session_dispatch():
    async def scenario():
      async with fixture() as (factory, workspace, canvas):
        from backend.infrastructure.orm import Outbox
        async with factory.begin() as tx:
            session, _ = await resolve_session(tx, workspace_id=workspace, user_id="outbox", document_id=canvas)
            await enqueue_message(tx, session, client_id="c", client_message_id="m",
                content={"text": "狗"})
            kinds = list(await tx.scalars(select(Outbox.event_type).where(Outbox.aggregate_id == str(session.id))))
            assert "session.dispatch" not in kinds
            assert "core.changed" in kinds
    asyncio.run(scenario())


def test_same_run_new_plan_reuses_task_key():
    async def scenario():
      async with fixture() as (factory, workspace, canvas):
        from backend.core.contracts import TaskInput
        from backend.core.orm import CorePlan
        from backend.core.store import create_task
        async with factory.begin() as tx:
            session, _ = await resolve_session(tx, workspace_id=workspace, user_id="replan", document_id=canvas)
            run, plan, task = await _seed_run_task(tx, session, workspace, "replan")
            plan2 = CorePlan(run_id=run.id, version=2, writer="rule", proposal={}, content_hash="c" * 64,
                rationale={}, link_constraints={}, result_type="generate", stage="execute", milestones=[],
                self_check={}, expansion_rules={}, version_lock={}, input_revision=2)
            tx.add(plan2)
            await tx.flush()
            again = await create_task(tx, TaskInput(run_id=run.id, plan_id=plan2.id, plan_version=2,
                task_key="t1", kind="call_tool", idempotency_key=uuid.uuid4().hex, parameters={},
                scope={}, input_revision=2))
            assert again.id != task.id
            assert again.task_key == task.task_key
            assert again.plan_id != task.plan_id
    asyncio.run(scenario())


def test_deleted_active_run_cache_is_repaired_without_waiting():
    async def scenario():
      async with fixture() as (factory, workspace, canvas):
        async with factory.begin() as tx:
            session, _ = await resolve_session(tx, workspace_id=workspace, user_id="stale-cache", document_id=canvas)
            session.active_run_id = uuid.uuid4()
            result = await enqueue_message(tx, session, client_id="window", client_message_id="new",
                content={"text": "你好"})
            assert result.sequence == 1
            assert session.active_run_id is None
    asyncio.run(scenario())
