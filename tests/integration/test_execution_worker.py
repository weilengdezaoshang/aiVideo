"""All provider behavior is fake; storage, credits, states and outbox use real isolated PG."""
import asyncio
import os
import uuid
from pathlib import Path
from contextlib import asynccontextmanager

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from backend.agent.media.credits import grant
from backend.agent.orm import HostCreditAccount, HostCreditLedger
from backend.core.contracts import CoreSettings, RunInput, TaskInput
from backend.core.execution.contracts import WorkerSettings
from backend.core.execution.engine import Worker, enqueue, record_admission
from backend.core.execution.fake import FakeProvider, Scenario
from backend.core.execution.orm import CanvasWrite, TaskExecution
from backend.core.orm import CorePlan, CoreRun, CoreSession, CoreTask, CoreEvent
from backend.core.store import create_run, create_task, transition
from backend.core.tools import CatalogSnapshot, ToolCatalog
from backend.core.tools.contracts import Execution, Port, Price, ToolDefinition
from backend.core.tools.store import publish
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Asset, Document, Workspace

DB = os.environ.get("AIVERO_EXECUTION_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated execution PG required")


def settings():
    return WorkerSettings(ticket_key=Fernet.generate_key().decode(), lease_seconds=0.2,
        call_timeout_seconds=0.1, poll_seconds=0.001, retry_base_seconds=0.001,
        retry_max_seconds=0.01, retry_jitter=0)


def tool_catalog(idempotent=True, cancellable=True, artifact_type="image"):
    tool = ToolDefinition(name="fake_generate", version="v1", description="test only",
        parameters={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"], "additionalProperties": False},
        result_schema={"type": "object"}, inputs=[], outputs=[Port(path="output", media_type=artifact_type)],
        mode="generate", price=Price(kind="fixed", credits=10), limits={}, failures=[],
        execution=Execution(adapter="execution_test_fake", provider_idempotent=idempotent, cancellable=cancellable, cancellation="provider" if cancellable else "none", result_url_policy="authenticated_asset", sla_seconds=60))
    return ToolCatalog(CatalogSnapshot(version=uuid.uuid4().hex, tools=[tool]))


@asynccontextmanager
async def fixture(tmp_path, scenario=None, *, provider=None, document=None, artifact_type="image"):
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    factory = create_async_session_factory(engine)
    config = settings()
    fake = provider or FakeProvider(tmp_path / "fake.sqlite", scenario or Scenario(actual_credits=3))
    worker = Worker(factory, tmp_path / "media", fake, settings=config)
    catalog = tool_catalog(fake.idempotent, fake.cancellable, artifact_type)
    async with factory.begin() as tx:
        if document:
            doc = await tx.get(Document, document)
            workspace = doc.workspace_id
        else:
            workspace = uuid.uuid4()
            tx.add(Workspace(id=workspace))
            await tx.flush()
            doc = Document(workspace_id=workspace, data={"objects": {}, "order": []})
            tx.add(doc)
            await tx.flush()
        await grant(tx, workspace, uuid.uuid4().hex, 100, "test")
        await publish(tx, catalog, published_by="test")
        session = CoreSession(workspace_id=workspace, source_canvas_id=doc.id, canvas_id=doc.id, user_id="test", language="zh-CN")
        tx.add(session)
        await tx.flush()
        run = await create_run(tx, RunInput(session_id=session.id, workspace_id=workspace, user_id="test",
            request_key=uuid.uuid4().hex, request_hash="a" * 64, snapshot={},
            scope={"tools": ["fake_generate@v1"], "write_canvas": True}, invariants={}, version_lock={"tool_catalog": catalog.lock.model_dump()}), CoreSettings(credits_limit=100))
        await transition(tx, "run", run.id, 0, "planning")
        await transition(tx, "run", run.id, 1, "running")
        plan = CorePlan(run_id=run.id, version=1, writer="rule", proposal={}, content_hash="b" * 64,
            rationale={}, link_constraints={}, result_type="generate", stage="final", milestones=[], self_check={}, expansion_rules={}, version_lock={})
        tx.add(plan)
        await tx.flush()
        run.current_plan_id = plan.id
        task = await create_task(tx, TaskInput(run_id=run.id, plan_id=plan.id, plan_version=1,
            task_key="t1", kind="call_tool", tool_ref="fake_generate", tool_version="v1",
            idempotency_key="same-client-key-across-runs", parameters={"prompt": "red"}, scope={}))
        await transition(tx, "task", task.id, 0, "validated")
        await record_admission(tx, task, catalog, checks={"legal": True, "types": True, "intent": True},
                               review_ref=uuid.uuid4().hex, placement={"mode": "region", "region": {"x": 0, "y": 0, "width": 280, "height": 280}})
        await transition(tx, "task", task.id, 1, "queued")
        job_id = await enqueue(tx, task.id, config)
        task_id, run_id, doc_id = task.id, run.id, doc.id
    try:
        yield worker, factory, job_id, task_id, run_id, doc_id
    finally:
        await engine.dispose()


async def drive(worker, job_id, count=100):
    for _ in range(count):
        await worker.execute(job_id)
        async with worker.factory() as tx:
            record = await tx.scalar(select(TaskExecution).where(TaskExecution.job_id == job_id))
            if record.phase == "done":
                return
        await asyncio.sleep(0.005)
    raise AssertionError("execution did not finish")


def test_success_settles_and_publishes_once(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (worker, factory, job, task, run, doc):
            await drive(worker, job)
            await worker.execute(job)
            async with factory() as tx:
                assert (await tx.get(CoreTask, task)).status == "succeeded"
                assert (await tx.get(CoreTask, task)).actual_credits == 3
                assert (await tx.get(CoreRun, run)).credits_spent == 3
                assert len((await tx.get(Document, doc)).data["objects"]) == 1
                assert await tx.scalar(select(func.count()).select_from(CanvasWrite).where(CanvasWrite.task_id == task)) == 1
                ledger = await tx.scalar(select(HostCreditLedger).where(HostCreditLedger.core_task_id == task))
                assert (ledger.quoted, ledger.charged, ledger.refunded) == (10, 3, 7)
            assert worker.provider.counts()["submissions"] == 1
    asyncio.run(scenario())


def test_callbacks_three_times_and_lost_callback_polling(tmp_path):
    async def scenario():
        async with fixture(tmp_path, Scenario(complete_after_seconds=0.02, callback_copies=3, actual_credits=3)) as (worker, factory, job, task, run, doc):
            await worker.execute(job)
            await asyncio.sleep(0.03)
            async with factory() as tx:
                from backend.core.orm import CoreTaskAttempt
                record = await tx.get(TaskExecution, task)
                attempt = await tx.get(CoreTaskAttempt, record.attempt_id)
                scope = attempt.provider_ref
            for _, result in await worker.provider.callbacks():
                assert await worker.callback(job, scope, result)
            await drive(worker, job)
            for _, result in await worker.provider.callbacks():
                assert await worker.callback(job, scope, result)
            async with factory() as tx:
                assert len((await tx.get(Document, doc)).data["objects"]) == 1
        async with fixture(tmp_path / "lost", Scenario(callback_copies=0, actual_credits=3)) as (worker, factory, job, task, run, doc):
            await drive(worker, job)
            assert await worker.provider.callbacks() == []
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome,status", [("blocked_by_model", "blocked_by_model"), ("param_invalid", "failed"), ("retryable", "failed")])
def test_failures_refund_and_obey_retry_limit(tmp_path, outcome, status):
    async def scenario():
        async with fixture(tmp_path, Scenario(outcome=outcome)) as (worker, factory, job, task, run, doc):
            await drive(worker, job)
            async with factory() as tx:
                assert (await tx.get(CoreTask, task)).status == status
                account = (await tx.scalars(select(HostCreditAccount).join(CoreRun, CoreRun.workspace_id == HostCreditAccount.workspace_id).where(CoreRun.id == run))).one()
                assert account.available == 100
                assert not (await tx.get(Document, doc)).data["objects"]
                retry_count = (await tx.get(CoreTask, task)).retry_count
            assert worker.provider.counts()["submissions"] == (retry_count + 1 if outcome == "retryable" else 1)
    asyncio.run(scenario())


def test_uncancellable_finishes_in_history_and_is_charged(tmp_path):
    async def scenario():
        async with fixture(tmp_path, Scenario(cancellable=False, complete_after_seconds=0.03, actual_credits=4)) as (worker, factory, job, task, run, doc):
            await worker.execute(job)
            assert await worker.cancel(job)
            await drive(worker, job)
            async with factory() as tx:
                assert (await tx.get(CoreTask, task)).status == "cancelled"
                assert (await tx.get(CoreTask, task)).actual_credits == 4
                assert not (await tx.get(Document, doc)).data["objects"]
                assert (await tx.get(CanvasWrite, task)).history_only
                assert (await tx.get(Asset, (await tx.get(CanvasWrite, task)).asset_id)).details["history"]
    asyncio.run(scenario())


def test_two_runs_same_region_conflict_falls_back(tmp_path):
    async def scenario():
        async with fixture(tmp_path / "a") as (a, af, aj, at, ar, doc):
            async with fixture(tmp_path / "b", document=doc) as (b, bf, bj, bt, br, _):
                await asyncio.gather(drive(a, aj), drive(b, bj))
                async with af() as tx:
                    objects = list((await tx.get(Document, doc)).data["objects"].values())
                    assert len(objects) == 2
                    from backend.core.execution.layout import overlaps
                    assert not overlaps(*objects)
                    conflicts = await tx.scalar(select(func.count()).select_from(CoreEvent).where(CoreEvent.run_id.in_([ar, br]), CoreEvent.event_type == "conflict"))
                    assert conflicts == 1
    asyncio.run(scenario())


def test_timeout_after_acceptance_recovers_without_idempotency(tmp_path):
    async def scenario():
        async with fixture(tmp_path, Scenario(outcome="timeout", idempotent=False, query_registration=True, actual_credits=3)) as (worker, factory, job, task, run, doc):
            await drive(worker, job)
            assert worker.provider.counts()["submissions"] == 1
    asyncio.run(scenario())


def test_unknown_nonqueryable_never_retries_or_refunds(tmp_path):
    async def scenario():
        async with fixture(tmp_path, Scenario(outcome="timeout", idempotent=False, query_registration=False)) as (worker, factory, job, task, run, doc):
            for _ in range(5):
                await worker.execute(job)
                await asyncio.sleep(0.005)
            assert worker.provider.counts()["submissions"] == 1
            async with factory() as tx:
                assert (await tx.get(TaskExecution, task)).phase == "unknown"
                assert (await tx.scalar(select(HostCreditLedger).where(HostCreditLedger.core_task_id == task))).state == "reserved"
    asyncio.run(scenario())


def test_sixty_second_link_is_owned_before_expiry_and_download_retries(tmp_path):
    async def scenario():
        async with fixture(tmp_path, Scenario(link_ttl_seconds=60, download_failures=2, actual_credits=3)) as (worker, factory, job, task, run, doc):
            await drive(worker, job)
            callbacks = await worker.provider.callbacks()
            expires = callbacks[0][1].expires_at
            async with factory() as tx:
                record = await tx.get(TaskExecution, task)
                asset = await tx.get(Asset, record.asset_id)
                assert asset.created_at.timestamp() < expires
                assert record.ticket is None
                assert not any("http" in str(value) for value in (asset.storage_key, (await tx.get(CoreTask, task)).output))
                assert worker.spool.verify(record.artifact) is None
    asyncio.run(scenario())


def crash_after_submit(database, fake_path, media_root, job_id, config_json):
    """Spawned process owns its own engine and connections; fake service commits independently."""
    import signal
    class KilledProvider(FakeProvider):
        async def submit(self, *args):
            result = await super().submit(*args)
            os.kill(os.getpid(), signal.SIGKILL)
            return result
    async def child():
        engine = create_async_database_engine(DatabaseSettings(url=database))
        factory = create_async_session_factory(engine)
        provider = KilledProvider(Path(fake_path), Scenario(idempotent=False, actual_credits=3))
        worker = Worker(factory, Path(media_root), provider, settings=WorkerSettings.model_validate_json(config_json))
        await worker.execute(uuid.UUID(job_id))
    asyncio.run(child())


def test_sigkill_after_submit_before_provider_id_record_recovers_once(tmp_path):
    import multiprocessing
    async def scenario():
        async with fixture(tmp_path, Scenario(idempotent=False, actual_credits=3)) as (worker, factory, job, task, run, doc):
            process = multiprocessing.get_context("spawn").Process(target=crash_after_submit, args=(DB,
                str(tmp_path / "fake.sqlite"), str(tmp_path / "media"), str(job), worker.settings.model_dump_json()))
            process.start()
            await asyncio.to_thread(process.join, 10)
            assert process.exitcode == -9
            # The lease is a real deadline, not a test-side rewrite of execution state.
            await asyncio.sleep(worker.settings.lease_seconds)
            await drive(worker, job)
            assert worker.provider.counts()["submissions"] == 1
            async with factory() as tx:
                ledger = await tx.scalar(select(HostCreditLedger).where(HostCreditLedger.core_task_id == task))
                assert (ledger.charged, ledger.refunded) == (3, 7)
                assert len((await tx.get(Document, doc)).data["objects"]) == 1
    asyncio.run(scenario())


def test_cancellable_pending_refunds_without_publication(tmp_path):
    async def scenario():
        async with fixture(tmp_path, Scenario(complete_after_seconds=10)) as (worker, factory, job, task, run, doc):
            await worker.execute(job)
            await worker.cancel(job)
            await drive(worker, job)
            async with factory() as tx:
                assert (await tx.get(CoreTask, task)).status == "cancelled"
                ledger = await tx.scalar(select(HostCreditLedger).where(HostCreditLedger.core_task_id == task))
                assert (ledger.charged, ledger.refunded) == (0, 10)
                assert not (await tx.get(Document, doc)).data["objects"]
    asyncio.run(scenario())


def test_after_result_and_terminal_state_roll_back_together(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (worker, factory, job, task, run, doc):
            async def broken_after(task, asset):
                raise RuntimeError("crash before transaction commit")
            worker.after = broken_after
            with pytest.raises(RuntimeError):
                await worker.execute(job)
            async with factory() as tx:
                assert not (await tx.get(Document, doc)).data["objects"]
                assert await tx.get(CanvasWrite, task) is None
                assert (await tx.scalar(select(HostCreditLedger).where(HostCreditLedger.core_task_id == task))).state == "reserved"
            await asyncio.sleep(worker.settings.lease_seconds)
            from backend.core.execution.contracts import after_result
            worker.after = after_result
            await drive(worker, job)
            assert worker.provider.counts()["submissions"] == 1
    asyncio.run(scenario())


def test_signed_callback_ingress_and_scoped_cost_api(tmp_path, monkeypatch):
    import hashlib
    import hmac
    import json
    from dataclasses import asdict
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI
    from backend.core.execution.api import install
    from backend.services.identity import Identity, authenticated

    async def scenario():
        async with fixture(tmp_path, Scenario(complete_after_seconds=0.02, actual_credits=3)) as (worker, factory, job, task, run, doc):
            await worker.execute(job)
            monkeypatch.setenv("AIVERO_EXECUTION_TICKET_KEY", worker.settings.ticket_key)
            secret = "callback-test-secret" * 2
            monkeypatch.setenv("AIVERO_EXECUTION_CALLBACK_SECRET", secret)
            async with factory() as tx:
                workspace = (await tx.get(CoreRun, run)).workspace_id
                from backend.core.orm import CoreTaskAttempt
                scope = (await tx.get(CoreTaskAttempt, (await tx.get(TaskExecution, task)).attempt_id)).provider_ref
            app = FastAPI()
            app.state.factory, app.state.settings = factory, SimpleNamespace(media_root=tmp_path / "media")
            install(app)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://test") as client:
                assert (await client.get(f"/api/core/runs/{run}/costs")).status_code == 401
                app.dependency_overrides[authenticated] = lambda: Identity("other", workspace, "owner")
                assert (await client.get(f"/api/core/runs/{run}/costs")).status_code == 404
                app.dependency_overrides[authenticated] = lambda: Identity("test", workspace, "owner")
                assert (await client.get(f"/api/core/runs/{run}/costs")).json()["entries"][0]["settlement"] == "reserved"
                await asyncio.sleep(0.03)
                result = (await worker.provider.callbacks())[0][1]
                data = json.dumps({"provider_ref": scope, "result": asdict(result)}).encode()
                path = f"/api/core/provider-callbacks/{job}"
                assert (await client.post(path, content=data)).status_code == 403
                signature = hmac.new(secret.encode(), str(job).encode() + b"\n" + data, hashlib.sha256).hexdigest()
                for _ in range(3):
                    assert (await client.post(path, content=data, headers={"x-provider-signature": signature})).status_code == 200
                malformed = b"[]"
                bad_signature = hmac.new(secret.encode(), str(job).encode() + b"\n" + malformed, hashlib.sha256).hexdigest()
                assert (await client.post(path, content=malformed, headers={"x-provider-signature": bad_signature})).status_code == 400
                await drive(worker, job)
                cost = (await client.get(f"/api/core/runs/{run}/costs")).json()
                assert cost["actual_credits"] == 3 and cost["entries"][0]["refunded"] == 7
                from backend.core.execution.orm import ProviderReceipt
                async with factory() as tx:
                    assert (await tx.scalar(select(ProviderReceipt).where(ProviderReceipt.task_id == task))).ticket is None
    asyncio.run(scenario())


def test_preparation_checkpoint_retries_without_submitting_twice(tmp_path):
    class PreparingFake(FakeProvider):
        prepares = 0
        async def prepare(self, params):
            self.prepares += 1
            if self.prepares == 1:
                raise OSError("upload temporarily unavailable, not a paid submission")
            return {"uploaded": True}
        async def submit(self, registration, params, idem):
            assert params["_prepared"] == {"uploaded": True}
            return await super().submit(registration, params, idem)
    async def scenario():
        fake = PreparingFake(tmp_path / "fake.sqlite", Scenario(actual_credits=3))
        async with fixture(tmp_path, provider=fake) as (worker, factory, job, task, run, doc):
            await drive(worker, job)
            assert fake.prepares == 2 and fake.counts()["submissions"] == 1
    asyncio.run(scenario())


def test_missing_admission_and_hook_rewrite_cannot_spend(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (worker, factory, job, task, run, doc):
            async def rewrite(task):
                return {"prompt": "unapproved change"}
            worker.before = rewrite
            with pytest.raises(ValueError, match="fresh_admission"):
                await worker.execute(job)
            assert worker.provider.counts()["submissions"] == 0
            async with factory() as tx:
                assert await tx.scalar(select(HostCreditLedger).where(HostCreditLedger.core_task_id == task)) is None
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["success", "param_invalid"])
def test_video_is_preserved_as_video_and_never_degrades_to_image(tmp_path, outcome):
    async def scenario():
        async with fixture(tmp_path, Scenario(outcome=outcome, actual_credits=3), artifact_type="video") as (worker, factory, job, task, run, doc):
            await drive(worker, job)
            async with factory() as tx:
                record = await tx.get(CoreTask, task)
                if outcome == "success":
                    assert record.status == "succeeded" and record.output["media_type"] == "video"
                    asset = await tx.get(Asset, uuid.UUID(record.output["asset_id"]))
                    assert asset.kind == "video" and asset.ext == "mp4"
                else:
                    assert record.status == "failed" and record.output is None
                    assert not (await tx.get(Document, doc)).data["objects"]
            assert worker.provider.counts()["submissions"] == 1
    asyncio.run(scenario())


def test_locked_canvas_does_not_lose_provider_handle_or_result(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (worker, factory, job, task, run, doc):
            async with factory.begin() as editor:
                await editor.get(Document, doc, with_for_update=True)
                result = await worker.execute(job)
                assert result["deferred"] == "publish_busy"
                async with factory() as tx:
                    from backend.core.orm import CoreTaskAttempt
                    execution = await tx.get(TaskExecution, task)
                    assert execution.artifact
                    assert (await tx.get(CoreTaskAttempt, execution.attempt_id)).provider_task_id
            await drive(worker, job)
            assert worker.provider.counts()["submissions"] == 1
            async with factory() as tx:
                assert len((await tx.get(Document, doc)).data["objects"]) == 1
    asyncio.run(scenario())
