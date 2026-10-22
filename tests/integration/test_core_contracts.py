"""Opt-in real PostgreSQL contract tests: no providers, Redis, queues or paid calls."""
import asyncio
from contextlib import asynccontextmanager
import os
import uuid

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from backend.core.contracts import AssetContent, CoreSettings, RunInput, TaskInput
from backend.core.orm import (CoreAsset, CoreAssetLineage, CoreAssetVersion, CoreEvent, CorePlan,
    CoreRun, CoreSession, CoreSkill, CoreSkillVersion, CoreTask, CoreTaskDependency)
from backend.core.states import RUN_EDGES, TASK_EDGES, InvalidTransition, VersionConflict
from backend.core.store import append_asset_version, create_asset, create_run, create_task, transition
from backend.infrastructure.database import (DatabaseSettings, create_async_database_engine,
    create_async_session_factory)
from backend.infrastructure.orm import Workspace

DB = os.environ.get("AIVERO_CORE_TEST_DB") or os.environ.get("AIVERO_DURABLE_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated PostgreSQL required")


@asynccontextmanager
async def fixture():
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    factory = create_async_session_factory(engine)
    workspace = uuid.uuid4()
    async with factory.begin() as db:
        db.add(Workspace(id=workspace))
        await db.flush()
        session = CoreSession(workspace_id=workspace, source_canvas_id=uuid.uuid4(), user_id="test-user", language="zh-CN")
        db.add(session)
        await db.flush()
        run = await create_run(db, RunInput(session_id=session.id, workspace_id=workspace, user_id="test-user",
            request_key=uuid.uuid4().hex, request_hash="a" * 64, snapshot={}, scope={}, invariants={}, version_lock={}))
        ident = run.id
    try:
        yield factory, workspace, session.id, ident
    finally:
        await engine.dispose()


async def plan_and_task(db, run_id, kind="call_tool", plan_id=None):
    if plan_id is None:
        plan = CorePlan(run_id=run_id, version=1, writer="rule", proposal={}, content_hash="b" * 64,
            rationale={}, link_constraints={}, result_type="generate", stage="draft", milestones=[],
            self_check={}, expansion_rules={}, version_lock={})
        db.add(plan)
        await db.flush()
        plan_id = plan.id
    return await create_task(db, TaskInput(run_id=run_id, plan_id=plan_id, plan_version=1,
        task_key=uuid.uuid4().hex, kind=kind, idempotency_key=uuid.uuid4().hex, parameters={}, scope={}))


async def move_run(db, run_id, state, *, resume="running"):
    path = {"created": [], "planning": ["planning"], "awaiting_confirm": ["planning", "awaiting_confirm"],
        "running": ["planning", "running"], "paused": ["planning"] + ([] if resume == "planning" else [resume]) + ["paused"]}[state]
    version = 0
    for target in path:
        await transition(db, "run", run_id, version, target, reason="waiting_user" if target == "paused" else None)
        version += 1
    return version


async def move_task(db, task_id, state):
    path = {"pending": [], "validated": ["validated"], "queued": ["validated", "queued"],
        "submitted": ["validated", "queued", "submitted"], "waiting_user": ["validated", "waiting_user"]}[state]
    version = 0
    for target in path:
        await transition(db, "task", task_id, version, target)
        version += 1
    return version


RUN_CASES = [(s, t) for s, targets in RUN_EDGES.items() for t in sorted(targets)]
TASK_CASES = [(s, t) for s, targets in TASK_EDGES.items() for t in sorted(targets)]


@pytest.mark.parametrize("source,target", RUN_CASES)
def test_each_run_edge_is_atomic_in_postgresql(source, target):
    async def scenario():
        async with fixture() as (factory, _, __, ident):
            async with factory.begin() as db:
                version = await move_run(db, ident, source, resume=target if target in {"planning", "awaiting_confirm", "running"} else "running")
                result = await transition(db, "run", ident, version, target,
                    reason="waiting_user" if target == "paused" else None)
                assert result.from_state == source and result.to_state == target and result.version == version + 1
            async with factory() as db:
                run = await db.get(CoreRun, ident)
                event = await db.get(CoreEvent, result.event_id)
                assert run.status == target and run.version == result.version
                assert event.run_sequence == run.event_cursor and event.payload == {"run_id": str(ident)}
                assert event.delivery_status == "pending"
    asyncio.run(scenario())


@pytest.mark.parametrize("source,target", TASK_CASES)
def test_each_task_edge_in_postgresql(source, target):
    async def scenario():
        async with fixture() as (factory, _, __, ident):
            async with factory.begin() as db:
                task = await plan_and_task(db, ident, "wait_user" if "waiting_user" in {source, target} else "call_tool")
                version = await move_task(db, task.id, source)
                result = await transition(db, "task", task.id, version, target)
                assert result.from_state == source and task.status == target
                event = await db.get(CoreEvent, result.event_id)
                assert event.task_id == task.id and event.plan_version == 1
                assert set(event.payload) == {"run_id", "task_id", "plan_id"}
    asyncio.run(scenario())


@pytest.mark.parametrize("entity,target", [("run", x) for x in
    ["created", "running", "awaiting_confirm", "paused", "completed", "invalid"]] + [("task", x) for x in
    ["pending", "queued", "submitted", "waiting_user", "succeeded", "failed", "blocked_by_model", "invalid"]])
def test_illegal_transitions_leave_no_event_or_state_change(entity, target):
    async def scenario():
        async with fixture() as (factory, _, __, ident):
            async with factory.begin() as db:
                row = await plan_and_task(db, ident) if entity == "task" else await db.get(CoreRun, ident)
                count = await db.scalar(select(func.count()).select_from(CoreEvent).where(CoreEvent.run_id == ident))
                with pytest.raises(InvalidTransition):
                    await transition(db, entity, row.id, 0, target)
                await db.refresh(row)
                assert row.version == 0
                assert await db.scalar(select(func.count()).select_from(CoreEvent).where(CoreEvent.run_id == ident)) == count
    asyncio.run(scenario())


def test_concurrent_run_cas_has_exactly_one_winner():
    async def scenario():
        async with fixture() as (factory, _, __, ident):
            async def writer():
                try:
                    async with factory.begin() as db:
                        return await transition(db, "run", ident, 0, "planning")
                except VersionConflict:
                    return "conflict"
            outcomes = await asyncio.gather(writer(), writer())
            assert outcomes.count("conflict") == 1
            async with factory() as db:
                assert (await db.get(CoreRun, ident)).version == 1
                assert await db.scalar(select(func.count()).select_from(CoreEvent).where(CoreEvent.run_id == ident)) == 2
    asyncio.run(scenario())


def test_outer_rollback_and_event_failure_roll_back_state():
    async def scenario():
        async with fixture() as (factory, _, __, ident):
            with pytest.raises(RuntimeError):
                async with factory.begin() as db:
                    await transition(db, "run", ident, 0, "planning")
                    raise RuntimeError("crash before commit")
            async with factory.begin() as db:
                assert (await db.get(CoreRun, ident)).status == "created"
                # Missing causation FK fails AFTER the state trigger has performed its update.
                with pytest.raises(DBAPIError):
                    await transition(db, "run", ident, 0, "planning", causation_id=uuid.uuid4())
                await db.refresh(await db.get(CoreRun, ident))
                assert (await db.get(CoreRun, ident)).version == 0
                assert await db.scalar(select(func.count()).select_from(CoreEvent).where(CoreEvent.run_id == ident)) == 1
    asyncio.run(scenario())


def test_direct_state_updates_are_rejected_and_events_are_immutable():
    async def scenario():
        async with fixture() as (factory, _, __, ident):
            async with factory.begin() as db:
                task_id = (await plan_and_task(db, ident)).id
            for statement in [update(CoreTask).where(CoreTask.id == task_id).values(status="validated"),
                              update(CoreTask).where(CoreTask.id == task_id).values(kind="wait_user"),
                              update(CoreRun).where(CoreRun.id == ident).values(status="running"),
                              update(CoreRun).where(CoreRun.id == ident).values(version=2),
                              update(CoreEvent).where(CoreEvent.run_id == ident).values(to_state="failed")]:
                with pytest.raises(DBAPIError):
                    async with factory.begin() as db:
                        await db.execute(statement)
    asyncio.run(scenario())


def content(**changes):
    return AssetContent(kind="image", category="artifact", storage={"media_host": "existing", "key": "test.png"},
        content_hash="c" * 64, user_id="test-user", **changes)


def test_asset_append_preserves_prior_content_and_lineage():
    async def scenario():
        async with fixture() as (factory, workspace, _, ident):
            async with factory.begin() as db:
                task = await plan_and_task(db, ident)
                asset = await create_asset(db, workspace, "test-user", "personal", content(run_id=ident, task_id=task.id))
                old_id, asset_id = asset.current_version_id, asset.id
            async with factory.begin() as db:
                new = await append_asset_version(db, asset_id, 1, content(run_id=ident, task_id=task.id, starred=True), parents=(old_id,))
                assert new.version == 2 and new.previous_version_id == old_id
                assert (await db.get(CoreAssetVersion, old_id)).starred is False
                assert (await db.get(CoreAsset, asset_id)).current_version_id == new.id
                assert await db.scalar(select(func.count()).select_from(CoreAssetLineage).where(CoreAssetLineage.output_version_id == new.id)) == 1
                with pytest.raises(VersionConflict):
                    await append_asset_version(db, asset_id, 1, content())
            with pytest.raises(DBAPIError):
                async with factory.begin() as db:
                    await db.execute(update(CoreAssetVersion).where(CoreAssetVersion.id == old_id).values(starred=True))
    asyncio.run(scenario())


def test_task_guards_pause_resume_and_terminal_states():
    async def scenario():
        async with fixture() as (factory, _, __, ident):
            async with factory.begin() as db:
                wait = await plan_and_task(db, ident, "wait_user")
                await transition(db, "task", wait.id, 0, "validated")
                with pytest.raises(InvalidTransition):
                    await transition(db, "task", wait.id, 1, "queued")
                await move_run(db, ident, "paused", resume="running")
                with pytest.raises(InvalidTransition):
                    await transition(db, "run", ident, 3, "planning")
                await transition(db, "run", ident, 3, "cancelled")
                with pytest.raises(InvalidTransition):
                    await transition(db, "run", ident, 4, "planning")
    asyncio.run(scenario())


def test_sibling_tasks_have_one_contiguous_run_event_sequence():
    async def scenario():
        async with fixture() as (factory, _, __, ident):
            async with factory.begin() as db:
                first = await plan_and_task(db, ident)
                second = await plan_and_task(db, ident, plan_id=first.plan_id)
                tasks = [first.id, second.id]
            async def writer(task_id):
                async with factory.begin() as db:
                    return await transition(db, "task", task_id, 0, "validated")
            await asyncio.gather(*(writer(task_id) for task_id in tasks))
            async with factory() as db:
                sequence = list(await db.scalars(select(CoreEvent.run_sequence).where(CoreEvent.run_id == ident).order_by(CoreEvent.run_sequence)))
                assert sequence == list(range(1, 6))
    asyncio.run(scenario())


def test_plan_and_skill_versions_cannot_be_overwritten():
    async def scenario():
        async with fixture() as (factory, workspace, _, ident):
            async with factory.begin() as db:
                task = await plan_and_task(db, ident)
                plan_id = task.plan_id
                skill = CoreSkill(workspace_id=workspace, user_id="test-user", name="test")
                db.add(skill)
                await db.flush()
                skill_version = CoreSkillVersion(skill_id=skill.id, version=1, scenario="test", tool_allowlist=[],
                    phases=[], confirmation_points=[], boundaries={}, source="official", content_hash="d" * 64)
                db.add(skill_version)
                await db.flush()
                skill_id = skill_version.id
            for model, record_id in [(CorePlan, plan_id), (CoreSkillVersion, skill_id)]:
                with pytest.raises(DBAPIError):
                    async with factory.begin() as db:
                        await db.execute(update(model).where(model.id == record_id).values(content_hash="e" * 64))
    asyncio.run(scenario())


def test_cross_plan_dependency_is_rejected():
    async def scenario():
        async with fixture() as (factory, _, __, ident):
            async with factory.begin() as db:
                first = await plan_and_task(db, ident)
                plan = CorePlan(run_id=ident, version=2, writer="rule", proposal={}, content_hash="f" * 64,
                    rationale={}, link_constraints={}, result_type="generate", stage="final", milestones=[],
                    self_check={}, expansion_rules={}, version_lock={})
                db.add(plan)
                await db.flush()
                second = await create_task(db, TaskInput(run_id=ident, plan_id=plan.id, plan_version=2,
                    task_key="second", kind="call_tool", idempotency_key="second", parameters={}, scope={}))
                ids = first.id, second.id, plan.id
            with pytest.raises(DBAPIError):
                async with factory.begin() as db:
                    db.add(CoreTaskDependency(task_id=ids[1], depends_on_id=ids[0], run_id=ident, plan_id=ids[2]))
    asyncio.run(scenario())


def test_budget_constraints_and_creation_events_roll_back_together():
    async def scenario():
        async with fixture() as (factory, workspace, session, _):
            with pytest.raises(DBAPIError):
                async with factory.begin() as db:
                    await create_run(db, RunInput(session_id=session, workspace_id=workspace, user_id="test-user",
                        request_key="negative-budget", request_hash="a" * 64, snapshot={}, scope={}, invariants={}, version_lock={}),
                        CoreSettings(credits_limit=1))
                    await db.execute(update(CoreRun).where(CoreRun.session_id == session).values(credits_spent=2))
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(CoreRun).where(CoreRun.request_key == "negative-budget")) == 0
                assert await db.scalar(select(func.count()).select_from(CoreEvent).join(CoreRun, CoreRun.id == CoreEvent.run_id).where(CoreRun.session_id == session)) == 1
    asyncio.run(scenario())


def test_schema_has_required_indexes_and_no_core_rows_in_legacy_outbox():
    async def scenario():
        async with fixture() as (factory, _, __, ident):
            async with factory() as db:
                names = set(await db.scalars(text("SELECT indexname FROM pg_indexes WHERE tablename LIKE 'core_%'")))
                assert {"ix_core_run_parent", "ix_core_run_session", "ix_core_run_scope", "ix_core_run_budget",
                    "ix_core_run_token_budget", "ix_core_run_thought_budget", "ix_core_run_version_lock",
                    "ix_core_run_invariants", "ix_core_run_heartbeat", "ix_core_plan_stage", "ix_core_plan_milestones"} <= names
                assert await db.scalar(text("SELECT count(*) FROM outbox WHERE aggregate_id = :id"), {"id": str(ident)}) == 0
                assert (await db.get(CoreRun, ident)).event_cursor == 1
    asyncio.run(scenario())
