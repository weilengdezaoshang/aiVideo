"""Opt-in PostgreSQL coverage. Requires AIVERO_CORE_TEST_DB or AIVERO_DURABLE_TEST_DB.

The database must already be migrated through 0025. No paid providers.
"""
import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

DB = os.environ.get("AIVERO_CORE_TEST_DB") or os.environ.get("AIVERO_DURABLE_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated PostgreSQL required")


def _image(user, stored, digest, *, run_id=None, task_id=None):
    from backend.core.contracts import AssetContent
    return AssetContent(kind="image", category="artifact", storage={"asset_id": str(stored)},
                        stored_asset_id=stored, content_hash=digest, user_id=user, run_id=run_id, task_id=task_id)


@pytest.mark.parametrize("natural", [False, True])
def test_pg_switch_edit_and_redo(tmp_path, natural):
    from backend.agent.media.credits import grant
    from backend.agent.orm import HostCreditLedger
    from backend.core.asset_edit import append_image_edit
    from backend.core.contracts import CoreSettings, RunInput, TaskInput
    from backend.core.execution.contracts import WorkerSettings
    from backend.core.execution.engine import Worker, enqueue
    from backend.core.execution.fake import FakeProvider, Scenario
    from backend.core.orm import CoreAssetLineage, CoreAssetVersion, CorePlan, CoreRun, CoreSession, CoreSessionMessage, CoreTask
    from backend.core.session_redo_service import RedoRequest, redo_session
    from backend.core.store import create_run, create_task, transition
    from backend.core.supervisor.contracts import Plan
    from backend.core.supervisor.engine import Supervisor
    from backend.core.tools.store import publish
    from backend.core.version_switch import SwitchCommand, VersionSwitchWorker, submit_switch
    from backend.core.version_switch_orm import VersionSwitchCommand
    from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
    from backend.infrastructure.orm import Asset, AssetReference, Document, Outbox, Workspace
    from cryptography.fernet import Fernet

    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        user = "redo-user"
        workspace = uuid.uuid4()
        try:
            async with factory.begin() as db:
                db.add(Workspace(id=workspace))
                await db.flush()
                await grant(db, workspace, uuid.uuid4().hex, 100, user)
                original = Asset(workspace_id=workspace, kind="image", ext="png", width=8, height=8, bytes=4,
                                 sha256="11" * 32, storage_key="orig", details={})
                second = Asset(workspace_id=workspace, kind="image", ext="png", width=8, height=8, bytes=4,
                               sha256="22" * 32, storage_key="v2", details={})
                edited = Asset(workspace_id=workspace, kind="image", ext="png", width=8, height=8, bytes=4,
                               sha256="33" * 32, storage_key="edit", details={})
                upload = Asset(workspace_id=workspace, kind="image", ext="png", width=8, height=8, bytes=4,
                               sha256="44" * 32, storage_key="upload", details={})
                doc_a = Document(workspace_id=workspace, revision=2, data={"objects": {}, "order": []})
                doc_b = Document(workspace_id=workspace, revision=2, data={"objects": {}, "order": []})
                db.add_all([original, second, edited, upload, doc_a, doc_b])
                await db.flush()
                db.add(AssetReference(asset_id=upload.id, owner_type="document", owner_id=str(doc_a.id)))
                db.add(AssetReference(asset_id=original.id, owner_type="document", owner_id=str(doc_a.id)))
                db.add(AssetReference(asset_id=second.id, owner_type="document", owner_id=str(doc_a.id)))
                from backend.core.tools import CatalogSnapshot, ToolCatalog
                from backend.core.tools.contracts import Execution, Port, Price, ToolDefinition
                catalog_version = "b9-" + uuid.uuid4().hex[:16]
                catalog = ToolCatalog(CatalogSnapshot(version=catalog_version, tools=[ToolDefinition(
                    name="fake_generate", version=catalog_version, description="test only",
                    parameters={"type": "object", "properties": {
                        "prompt": {"type": "string"},
                        "seed": {"type": "integer", "minimum": 0, "maximum": 20}},
                        "required": ["prompt"], "additionalProperties": False},
                    result_schema={"type": "object"}, inputs=[], outputs=[Port(path="output", media_type="image")],
                    mode="generate", price=Price(kind="fixed", credits=10), limits={}, failures=[],
                    execution=Execution(adapter="execution_test_fake", provider_idempotent=True, cancellable=True,
                        cancellation="provider", result_url_policy="authenticated_asset", sla_seconds=60,
                        binding={"seed_supported": True}))]))
                lock = await publish(db, catalog, published_by="block9")
                session = CoreSession(workspace_id=workspace, source_canvas_id=doc_a.id, canvas_id=doc_a.id,
                                      user_id=user, language="zh-CN", message_cursor=1)
                db.add(session)
                await db.flush()
                db.add(CoreSessionMessage(session_id=session.id, sequence=1, request_key="opening", role="user",
                                          content={"text": "兔子"}, processing_status="processed"))
                version_lock = {"supervisor": "llm-v1", "tool_catalog": lock.model_dump(mode="json")}
                source = await create_run(db, RunInput(
                    session_id=session.id, workspace_id=workspace, user_id=user, request_key=uuid.uuid4().hex,
                    request_hash="a" * 64, snapshot={"objects": {"old": {}}, "order": ["old"],
                        "request": {"message": "兔子"}, "assets": [{"asset_id": str(original.id), "owner_id": user, "kind": "image"}], "recent_assets": []},
                    scope={"tools": ["fake_generate"], "readable_assets": [], "write_canvas": True},
                    invariants={}, version_lock=version_lock, input_revision=1),
                    CoreSettings(credits_limit=20, tokens_limit=1000, max_thought_rounds=4))
                proposal = Plan(goal="兔子", writer="rule", steps=[{"id": "t1", "tool": "fake_generate",
                    "parameters": {"prompt": "兔子", "seed": 1}}]).model_dump(mode="json")
                plan = CorePlan(run_id=source.id, version=1, writer="rule", proposal=proposal, content_hash="b" * 64,
                                rationale={}, link_constraints={"approved_invariants": {}},
                                result_type="generate", stage="execute", milestones=[],
                                self_check={"verdict": "pass"}, expansion_rules={}, version_lock=version_lock)
                db.add(plan)
                await db.flush()
                source.current_plan_id = plan.id
                task = await create_task(db, TaskInput(
                    run_id=source.id, plan_id=plan.id, plan_version=1, task_key="t1", kind="call_tool",
                    tool_ref="fake_generate", tool_version=catalog.lock.version,
                    idempotency_key=uuid.uuid4().hex, parameters={"prompt": "兔子", "seed": 1}, scope=source.scope))
                for version, target in enumerate(("validated", "queued", "submitted", "succeeded")):
                    await transition(db, "task", task.id, version, target)
                for version, target in enumerate(("planning", "running", "completed")):
                    await transition(db, "run", source.id, version, target)
                from backend.core.store import create_asset
                asset = await create_asset(db, workspace, user, "personal", _image(user, original.id, original.sha256))
                await append_image_edit(db, tool_name="image_edit", resolved_asset_id=original.id, workspace_id=workspace,
                                        user_id=user, run_id=source.id, task_id=task.id,
                                        result=_image(user, second.id, second.sha256, run_id=source.id, task_id=task.id),
                                        expected_version=asset.version)
                older = await append_image_edit(db, tool_name="inpaint", resolved_asset_id=original.id, workspace_id=workspace,
                                                user_id=user, run_id=source.id, task_id=task.id,
                                                result=_image(user, edited.id, edited.sha256, run_id=source.id, task_id=task.id))
                parent = await db.scalar(select(CoreAssetLineage.input_version_id).where(
                    CoreAssetLineage.output_version_id == older.version_id))
                first = await db.scalar(select(CoreAssetVersion).where(
                    CoreAssetVersion.asset_id == asset.id, CoreAssetVersion.version == 1))
                assert parent == first.id
                doc_a.data = {"objects": {"n": {"assetId": str(original.id), "assetVersion": 1, "src": "/old"}}, "order": ["n"]}
                doc_b.data = {"objects": {"n": {"assetId": str(original.id), "assetVersion": 1, "src": "/old"}}, "order": ["n"]}
                key = uuid.uuid4().hex
                body = SwitchCommand(document_id=doc_a.id, node_id="n", expected_document_revision=2,
                                     expected_anchor_version=1, idempotency_key=key)
                first_cmd = await submit_switch(db, asset_id=asset.id, version_id=older.version_id,
                                                workspace_id=workspace, user_id=user, body=body)
                from backend.core.version_switch import SwitchRejected
                with pytest.raises(SwitchRejected, match="asset_not_authorized"):
                    await submit_switch(db, asset_id=asset.id, version_id=older.version_id,
                                        workspace_id=workspace, user_id="other-user", body=body)
                leaked = await db.scalar(select(VersionSwitchCommand).where(
                    VersionSwitchCommand.user_id == "other-user",
                    VersionSwitchCommand.idempotency_key == key))
                assert leaked is None
                body_b = body.model_copy(update={"document_id": doc_b.id, "idempotency_key": uuid.uuid4().hex})
                second_cmd = await submit_switch(db, asset_id=asset.id, version_id=older.version_id,
                                                 workspace_id=workspace, user_id=user, body=body_b)
                held_id, free_id = first_cmd.id, second_cmd.id
                asset_id, version_id = asset.id, older.version_id
                upload_id, source_id, plan_id, session_id = upload.id, source.id, plan.id, session.id
                doc_a_id, doc_b_id = doc_a.id, doc_b.id
                task_id = task.id
            async def one_upload():
                async with factory.begin() as db:
                    return await append_image_edit(
                        db, tool_name="image_edit", resolved_asset_id=upload_id, workspace_id=workspace,
                        user_id=user, run_id=source_id, task_id=task_id,
                        result=_image(user, edited.id, edited.sha256, run_id=source_id, task_id=task_id))
            uploaded = await asyncio.gather(one_upload(), one_upload())
            assert uploaded[0].core_asset_id == uploaded[1].core_asset_id
            async def one_switch():
                async with factory.begin() as db:
                    return await submit_switch(db, asset_id=asset_id, version_id=version_id,
                                               workspace_id=workspace, user_id=user, body=body)
            dupes = await asyncio.gather(one_switch(), one_switch())
            assert dupes[0].id == dupes[1].id == held_id
            worker = VersionSwitchWorker(factory, owner="block9")
            async with factory.begin() as hold:
                await hold.scalar(select(Document).where(Document.id == doc_a_id).with_for_update())
                await worker.run_once()
            async with factory() as db:
                held = await db.get(VersionSwitchCommand, held_id)
                free = await db.get(VersionSwitchCommand, free_id)
                assert free.status == "applied"
                assert held.status == "pending" and held.attempt >= 1
                assert (await db.get(Document, doc_a_id)).revision == 2
                assert (await db.get(Document, doc_b_id)).revision == 3
                note = await db.scalar(select(Outbox).where(Outbox.event_type == "core.changed",
                                                            Outbox.aggregate_id == str(doc_b_id)))
                assert note.payload["commandId"] == str(free_id)
            async with factory.begin() as db:
                command = await db.get(VersionSwitchCommand, held_id)
                command.status = "leased"
                command.lease_token = uuid.uuid4()
                command.lease_owner = str(command.lease_token)
                command.lease_until = (await db.scalar(select(func.clock_timestamp()))) + timedelta(seconds=60)
                stale = command.lease_token
            assert await worker._apply(held_id, uuid.uuid4()) is False
            async with factory() as db:
                assert (await db.get(Document, doc_a_id)).revision == 2
                assert (await db.get(VersionSwitchCommand, held_id)).lease_token == stale
            async with factory.begin() as db:
                await db.execute(text("UPDATE core_version_switch_commands SET lease_until = clock_timestamp() - interval '1 second' WHERE id = :id"),
                                 {"id": held_id})
            for _ in range(5):
                await worker.run_once()
            async with factory() as db:
                assert (await db.get(Document, doc_a_id)).revision == 3
                assert (await db.get(VersionSwitchCommand, held_id)).status == "applied"
            class Planner:
                calls = 0
                async def plan(self, context):
                    self.calls += 1
                    raise AssertionError("planner called")
            planner = Planner()
            async with factory.begin() as db:
                session = await db.get(CoreSession, session_id)
                before = session.message_cursor
                doc = await db.get(Document, doc_a_id)
                doc.data = {**doc.data, "marker": "live"}
                if natural:
                    from backend.core.sessions.intake import enqueue_message
                    from backend.core.sessions.contracts import SessionSettings
                    receipt = await enqueue_message(db, session, client_id="redo-browser", client_message_id="again",
                        content={"text": "再来一次", "budget": 20, "tokens": 1000}, settings=SessionSettings())
                    session.dispatch_at -= timedelta(seconds=10)
                    await db.flush()
                else:
                    run = await redo_session(db, session_id=session_id, user_id=user, workspace_id=workspace, body=RedoRequest(
                        request_key="redo-1", source_run_id=source_id, source_plan_id=plan_id, credits_limit=20, tokens_limit=1000))
            if natural:
                from backend.core.sessions.dispatch import dispatch_due
                assert await dispatch_due(factory)
            async with factory() as db:
                session = await db.get(CoreSession, session_id)
                run = await db.get(CoreRun, session.active_run_id)
                if natural:
                    original_message = await db.get(CoreSessionMessage, receipt.message_id)
                    assert original_message.processing_status == "processed"
                    assert original_message.run_id == run.id
                assert run.snapshot["marker"] == "live" and "old" not in run.snapshot["objects"]
                assert run.snapshot["request"]["message"] == "兔子"
                assert run.invariants == {}
                assert session.message_cursor == before + (3 if natural else 2)
                assert session.input_revision == run.input_revision
                roles = list(await db.scalars(select(CoreSessionMessage.role).where(
                    CoreSessionMessage.session_id == session_id).order_by(CoreSessionMessage.sequence)))
                assert roles[-2:] == ["user", "assistant"]
                new_run = run.id
            supervisor = Supervisor(factory, planner)
            review = None
            for _ in range(8):
                await supervisor.tick()
                async with factory() as db:
                    task = await db.scalar(select(CoreTask).where(CoreTask.run_id == new_run))
                    if task is not None:
                        break
                    from backend.core.supervisor.orm import Invocation
                    inv = await db.scalar(select(Invocation).where(Invocation.run_id == new_run))
                    review = None if inv is None else inv.review
            assert planner.calls == 0
            async with factory.begin() as db:
                task = await db.scalar(select(CoreTask).where(CoreTask.run_id == new_run))
                assert task is not None and task.status == "queued", review
                job = await enqueue(db, task.id, WorkerSettings(ticket_key=Fernet.generate_key().decode()))
                new_task = task.id
            fake = FakeProvider(tmp_path / "fake.sqlite", Scenario(actual_credits=3))
            worker_exec = Worker(factory, tmp_path / "media", fake, settings=WorkerSettings(
                ticket_key=Fernet.generate_key().decode(), lease_seconds=0.2, call_timeout_seconds=0.2,
                poll_seconds=0.001, retry_base_seconds=0.001, retry_max_seconds=0.01, retry_jitter=0))
            for _ in range(40):
                await worker_exec.execute(job)
                async with factory() as db:
                    if (await db.get(CoreTask, new_task)).status == "succeeded":
                        break
                await asyncio.sleep(0.01)
            async with factory() as db:
                assert (await db.get(CoreTask, new_task)).status == "succeeded"
                assert (await db.get(CoreRun, new_run)).credits_spent == 3
                assert await db.scalar(select(func.count()).select_from(HostCreditLedger).where(
                    HostCreditLedger.core_task_id == new_task)) == 1
            assert fake.counts()["submissions"] == 1 and planner.calls == 0
        finally:
            from backend.core.supervisor.engine import stop
            from backend.core.supervisor.orm import Invocation
            from sqlalchemy import update
            async with factory.begin() as db:
                runs = list(await db.scalars(select(CoreRun).where(CoreRun.workspace_id == workspace)))
                for owned in runs:
                    if owned.status not in {"completed", "failed", "cancelled"}:
                        await db.get(CoreSession, owned.session_id, with_for_update=True)
                        await stop(db, owned, "cancelled", "fixture cleanup", "fixture-cleanup:" + str(owned.id))
                if runs:
                    await db.execute(update(Invocation).where(Invocation.run_id.in_([r.id for r in runs]))
                        .values(status="done", lease_token=None, lease_until=None))
            await engine.dispose()

    asyncio.run(scenario())
