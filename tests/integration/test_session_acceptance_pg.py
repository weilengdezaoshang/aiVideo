"""Strict session acceptance cases. Isolated PostgreSQL; no weakened outcomes."""
import asyncio
import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.core.orm import (CorePlan, CoreRun, CoreRunInputRevision, CoreSession, CoreSessionMessage,
    CoreTask)
from backend.core.sessions.contracts import SessionSettings
from backend.core.sessions.correction import detect_correction
from backend.core.sessions.dispatch import dispatch_due
from backend.core.sessions.intake import enqueue_message
from backend.core.sessions.resolve import resolve_session
from backend.core.sessions.revisions import append_revision, effective_request
from backend.core.tools import build_catalog
from backend.core.tools.store import publish
from backend.infrastructure.database import (DatabaseSettings, create_async_database_engine,
    create_async_session_factory)
from backend.infrastructure.orm import Document, Workspace

DB = (os.environ.get("AIVERO_SUPERVISOR_TEST_DB") or os.environ.get("AIVERO_CORE_TEST_DB")
    or os.environ.get("AIVERO_DURABLE_TEST_DB"))
needs_pg = pytest.mark.skipif(not DB, reason="explicit isolated migrated PostgreSQL required")


def test_ordinary_rephrase_is_not_explicit_disagreement():
    assert detect_correction("换成蓝色") is False
    assert detect_correction("改成蓝色") is False
    assert detect_correction("不是这个意思") is True
    assert detect_correction("不对兔子") is True


def test_effective_request_keeps_server_metadata():
    rows = [
        SimpleNamespace(content={"text": "狗", "language": "zh-CN", "action": "tool", "tool": "edit",
            "selected_assets": ["a"]}),
        SimpleNamespace(content={"text": "再暗一点"}),
    ]
    request = effective_request(rows)
    assert request["messages"] == ["狗", "再暗一点"]
    assert request["language"] == "zh-CN"
    assert request["action"] == "tool"
    assert request["tool"] == "edit"
    assert request["selected_assets"] == ["a"]


@asynccontextmanager
async def fixture():
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    factory = create_async_session_factory(engine)
    workspace = uuid.uuid4()
    canvas = uuid.uuid4()
    async with factory.begin() as tx:
        tx.add(Workspace(id=workspace, name="session-accept"))
        await tx.flush()
        tx.add(Document(id=canvas, workspace_id=workspace, name="c", data={"objects": {}}, revision=1))
        ident = uuid.uuid4().hex
        catalog = build_catalog(Catalog(CatalogConfig(version=ident, environment="development",
            enabled=True, capabilities=[])), ident)
        await publish(tx, catalog, published_by="test")
    try:
        yield factory, workspace, canvas
    finally:
        from sqlalchemy import update
        from backend.core.supervisor.engine import stop
        from backend.core.supervisor.orm import Invocation
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


async def _seed_run(tx, session, workspace, user_id):
    from backend.core.contracts import CoreSettings, RunInput
    from backend.core.store import create_run
    run = await create_run(tx, RunInput(session_id=session.id, workspace_id=workspace, user_id=user_id,
        request_key=uuid.uuid4().hex, request_hash="a" * 64, snapshot={"request": {"message": "狗"}},
        scope={}, invariants={}, version_lock={}, input_revision=1), CoreSettings())
    session.active_run_id = run.id
    session.message_cursor += 1
    tx.add(CoreSessionMessage(session_id=session.id, sequence=session.message_cursor,
        request_key="opening-" + str(run.id), role="user", content={"text": "狗"},
        processing_status="processed", run_id=run.id, input_revision=1))
    await tx.flush()
    return run


async def _complete(tx, run):
    from backend.core.store import transition
    await transition(tx, "run", run.id, run.version, "planning")
    await tx.refresh(run)
    await transition(tx, "run", run.id, run.version, "running")
    await tx.refresh(run)
    await transition(tx, "run", run.id, run.version, "completed")
    await tx.refresh(run)
    return run


def _pipeline():
    path = Path(__file__).with_name("test_supervisor_pipeline.py")
    spec = importlib.util.spec_from_file_location("session_block9_pipeline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_pg
def test_concurrent_session_first_open_same_id():
    async def scenario():
        async with fixture() as (factory, workspace, canvas):
            async def open_one():
                async with factory.begin() as tx:
                    session, _doc = await resolve_session(tx, workspace_id=workspace, user_id="same",
                        document_id=canvas)
                    return session.id
            left, right = await asyncio.gather(open_one(), open_one())
            assert left == right
    asyncio.run(scenario())


@needs_pg
def test_two_clients_identical_local_message_ids_keep_order():
    async def scenario():
        async with fixture() as (factory, workspace, canvas):
            async with factory.begin() as tx:
                session, _doc = await resolve_session(tx, workspace_id=workspace, user_id="multi",
                    document_id=canvas)
                phone = await enqueue_message(tx, session, client_id="phone", client_message_id="local-1",
                    content={"text": "先画狗"})
                tablet = await enqueue_message(tx, session, client_id="tablet", client_message_id="local-1",
                    content={"text": "再画兔子"})
                assert phone.message_id != tablet.message_id
                assert [phone.sequence, tablet.sequence] == [1, 2]
                session_id = session.id
            async with factory() as tx:
                session = await tx.get(CoreSession, session_id)
                rows = list(await tx.scalars(select(CoreSessionMessage).where(
                    CoreSessionMessage.session_id == session.id).order_by(CoreSessionMessage.sequence)))
                assert [(row.client_id, row.client_message_id, row.sequence, row.content["text"]) for row in rows] == [
                    ("phone", "local-1", 1, "先画狗"),
                    ("tablet", "local-1", 2, "再画兔子"),
                ]
    asyncio.run(scenario())


@needs_pg
def test_locked_run_a_does_not_block_session_b_dispatch():
    async def scenario():
        async with fixture() as (factory, workspace, canvas):
            settings = SessionSettings(quiet_window_seconds=0.01)
            async with factory.begin() as tx:
                session_a, _ = await resolve_session(tx, workspace_id=workspace, user_id="runner-a",
                    document_id=canvas)
                session_b, _ = await resolve_session(tx, workspace_id=workspace, user_id="runner-b",
                    document_id=canvas)
                run_a = await _seed_run(tx, session_a, workspace, "runner-a")
                await enqueue_message(tx, session_a, client_id="c", client_message_id="hold",
                    content={"text": "会话甲继续", "budget": 10, "tokens": 100, "rounds": 2}, settings=settings)
                await enqueue_message(tx, session_b, client_id="c", client_message_id="go",
                    content={"text": "会话乙", "budget": 10, "tokens": 100, "rounds": 2}, settings=settings)
                session_a.dispatch_at = session_a.dispatch_at - timedelta(seconds=30)
                session_b.dispatch_at = session_b.dispatch_at - timedelta(seconds=1)
                held_run, other_id = run_a.id, session_b.id
            holder = factory()
            await holder.__aenter__()
            try:
                await holder.begin()
                locked = await holder.scalar(select(CoreRun).where(CoreRun.id == held_run).with_for_update())
                assert locked is not None
                assert await asyncio.wait_for(dispatch_due(factory, settings), timeout=5) is True
                assert await asyncio.wait_for(dispatch_due(factory, settings), timeout=5) is True
                async with factory() as tx:
                    other = await tx.get(CoreSession, other_id)
                    assert other.active_run_id is not None
                    assert other.active_run_id != held_run
                await holder.rollback()
            finally:
                await holder.__aexit__(None, None, None)
    asyncio.run(scenario())


@needs_pg
def test_user_message_before_worker_fences_stale_task(tmp_path):
    async def scenario():
        pipeline = _pipeline()
        async with pipeline.fixture(tmp_path, planner="chain") as (
                factory, supervisor, _drive, run_id, *_rest):
            events = _rest[4]
            queued = None
            for _ in range(40):
                await events()
                await supervisor.tick()
                from backend.core.execution.engine import scan_ready
                await scan_ready(factory)
                async with factory() as tx:
                    task = await tx.scalar(select(CoreTask).where(CoreTask.run_id == run_id,
                        CoreTask.status == "queued"))
                    if task is not None:
                        queued = task.id, task.job_id
                        break
            assert queued is not None
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, run_id)
                session = await tx.scalar(select(CoreSession).where(CoreSession.id == run.session_id)
                    .with_for_update())
                await enqueue_message(tx, session, client_id="c", client_message_id="before-submit",
                    content={"text": "暗一点"}, settings=SessionSettings(quiet_window_seconds=30))
            from backend.agent.orm import HostCreditLedger
            from sqlalchemy import func
            worker = pipeline.Worker(factory, _rest[1], pipeline.FakeProvider(
                tmp_path / "fence.sqlite", pipeline.Scenario(actual_credits=3)), settings=_rest[2])
            result = await worker.execute(queued[1])
            assert result.get("cancelled") == "obsolete_input_revision"
            async with factory() as tx:
                task = await tx.get(CoreTask, queued[0])
                assert task.status == "cancelled"
                assert (task.error or {}).get("code") == "obsolete_input_revision"
                assert await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(
                    HostCreditLedger.core_run_id == run_id)) == 0
    asyncio.run(scenario())


@needs_pg
def test_late_fake_planner_output_stored_not_executed(tmp_path):
    async def scenario():
        pipeline = _pipeline()

        class Late:
            def __init__(self, factory):
                self.factory = factory

            async def plan(self, context):
                async with self.factory.begin() as tx:
                    run = await tx.get(CoreRun, uuid.UUID(context["run_id"]))
                    run.input_revision = int(run.input_revision or 1) + 1
                plan = {"goal": "late-must-not-run", "steps": [{"id": "t1", "tool": "edit", "parameters": {
                    "prompt": "late", "image": {"asset_id": context["request"]["selected_assets"][0],
                        "media_type": "image"}}}]}
                return {"plan": plan, "invariants": context["invariants"], "confidence": 1, "tokens": 3,
                    "raw_output": {"late": True, "plan": plan}}

        async with pipeline.fixture(tmp_path, make_planner=Late) as (
                factory, supervisor, _drive, run_id, _workspace, _root, _config, fake, events, *_):
            await events()
            await supervisor.tick()
            async with factory() as tx:
                from backend.core.supervisor.orm import Invocation
                invocation = await tx.scalar(select(Invocation).where(Invocation.run_id == run_id))
                assert invocation is not None
                assert invocation.result is not None
                assert invocation.result["raw_output"]["late"] is True
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
                assert tasks == []
                plans = list(await tx.scalars(select(CorePlan).where(CorePlan.run_id == run_id,
                    CorePlan.stage == "execute")))
                assert plans == []
                assert fake.counts()["submissions"] == 0
    asyncio.run(scenario())


@needs_pg
def test_old_run_correction_negative_only_for_explicit_disagreement():
    async def scenario():
        async with fixture() as (factory, workspace, canvas):
            async with factory.begin() as tx:
                session, _doc = await resolve_session(tx, workspace_id=workspace, user_id="corr",
                    document_id=canvas)
                run = await _seed_run(tx, session, workspace, "corr")
                await _complete(tx, run)
                session.active_run_id = None
                ordinary = await enqueue_message(tx, session, client_id="c", client_message_id="blue",
                    content={"text": "换成蓝色"})
                explicit = await enqueue_message(tx, session, client_id="c", client_message_id="no",
                    content={"text": "不是这个意思"}, correction_of_run_id=run.id)
                blue = await tx.get(CoreSessionMessage, ordinary.message_id)
                disagreed = await tx.get(CoreSessionMessage, explicit.message_id)
                assert blue.correction_of_run_id is None
                assert (blue.content or {}).get("feedback") is not True
                assert (blue.content or {}).get("correction") is not True
                assert disagreed.correction_of_run_id == run.id
                assert disagreed.content["feedback"] is True
                assert disagreed.content["correction"] is True
    asyncio.run(scenario())


@needs_pg
def test_retry_identical_body_after_active_run_changes_replays_row():
    async def scenario():
        async with fixture() as (factory, workspace, canvas):
            async with factory.begin() as tx:
                session, _doc = await resolve_session(tx, workspace_id=workspace, user_id="hash",
                    document_id=canvas)
                first_run = await _seed_run(tx, session, workspace, "hash")
                original = await enqueue_message(tx, session, client_id="c", client_message_id="same",
                    content={"text": "不是这个意思"})
                stored = await tx.get(CoreSessionMessage, original.message_id)
                assert stored.correction_of_run_id == first_run.id
                await _complete(tx, first_run)
                second = await _seed_run(tx, session, workspace, "hash")
                assert session.active_run_id == second.id
                assert second.id != first_run.id
                again = await enqueue_message(tx, session, client_id="c", client_message_id="same",
                    content={"text": "不是这个意思"})
                assert again.replayed is True
                assert again.message_id == original.message_id
                assert stored.correction_of_run_id == first_run.id
    asyncio.run(scenario())


@needs_pg
def test_pending_burst_correction_targets_new_request_not_previous_run():
    async def scenario():
        async with fixture() as (factory, workspace, canvas):
            settings = SessionSettings(quiet_window_seconds=1.2)
            async with factory.begin() as tx:
                session, _doc = await resolve_session(tx, workspace_id=workspace, user_id="burst",
                    document_id=canvas)
                previous = await _seed_run(tx, session, workspace, "burst")
                await _complete(tx, previous)
                session.active_run_id = None
                await enqueue_message(tx, session, client_id="c", client_message_id="1",
                    content={"text": "狗"}, settings=settings)
                await enqueue_message(tx, session, client_id="c", client_message_id="2",
                    content={"text": "不对兔子"}, settings=settings)
                await enqueue_message(tx, session, client_id="c", client_message_id="3",
                    content={"text": "再暗一点"}, settings=settings)
                rows = list(await tx.scalars(select(CoreSessionMessage).where(
                    CoreSessionMessage.session_id == session.id, CoreSessionMessage.client_id == "c")
                    .order_by(CoreSessionMessage.sequence)))
                assert [row.content["text"] for row in rows] == ["狗", "不对兔子", "再暗一点"]
                assert all(row.correction_of_run_id is None for row in rows)
                assert all((row.content or {}).get("feedback") is not True for row in rows)
                assert all(row.run_id is None for row in rows)
    asyncio.run(scenario())


@needs_pg
def test_new_revision_merges_prior_messages_without_rewriting_membership():
    async def scenario():
        async with fixture() as (factory, workspace, canvas):
            async with factory.begin() as tx:
                session, _doc = await resolve_session(tx, workspace_id=workspace, user_id="rev",
                    document_id=canvas)
                run = await _seed_run(tx, session, workspace, "rev")
                opening = await tx.scalar(select(CoreSessionMessage).where(CoreSessionMessage.run_id == run.id))
                pending = await enqueue_message(tx, session, client_id="c", client_message_id="next",
                    content={"text": "再暗一点", "language": "en", "action": "tool", "tool": "edit"})
                message = await tx.get(CoreSessionMessage, pending.message_id)
                revision = await append_revision(tx, run, session, [message], initial=False)
                await tx.refresh(opening)
                assert opening.input_revision == 1
                assert message.input_revision == 2
                assert revision.revision == 2
                assert revision.message_ids == [str(message.id)]
                assert str(opening.id) not in revision.message_ids
                assert revision.effective_request["messages"] == ["狗", "再暗一点"]
                assert revision.effective_request["message"] == "狗 再暗一点"
                assert revision.effective_request["language"] == "en"
                assert revision.effective_request["action"] == "tool"
                assert revision.effective_request["tool"] == "edit"
                stored = await tx.scalar(select(CoreRunInputRevision).where(
                    CoreRunInputRevision.run_id == run.id, CoreRunInputRevision.revision == 2))
                assert stored.effective_request["messages"] == ["狗", "再暗一点"]
    asyncio.run(scenario())
