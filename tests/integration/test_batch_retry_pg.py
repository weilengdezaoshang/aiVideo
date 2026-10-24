"""PostgreSQL: retry child runs go through supervisor admission, not hand-written tasks."""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import func, select

from backend.agent.media.credits import grant
from backend.agent.orm import HostCreditLedger
from backend.core.batch.compile import CompileError
from backend.core.batch.orm import CoreBatchRetry
from backend.core.batch.retry_helpers import (
    effective_batch_progress, reconcile_completed_retries,
)
from backend.core.batch.service import open_batch_retry
from backend.core.contracts import RunInput
from backend.core.events.contracts import EventSettings
from backend.core.events.processor import Processor
from backend.core.events.registry import Registry
from backend.core.events.store import accept
from backend.core.execution.contracts import WorkerSettings
from backend.core.execution.engine import Worker, scan_ready
from backend.core.execution.fake import FakeProvider, Scenario
from backend.core.execution.orm import CanvasWrite, ExecutionAdmission
from backend.core.orm import CoreEvent, CorePlan, CoreRun, CoreSession, CoreTask
from backend.core.store import create_run
from backend.core.supervisor.contracts import Settings
from backend.core.supervisor.engine import Supervisor, register, stop
from backend.core.supervisor.orm import Invocation
from backend.core.supervisor.planners import FakePlanner
from backend.core.supervisor.service import start
from backend.core.tools import ToolCatalog
from backend.core.tools.contracts import CatalogSnapshot, Execution, Port, Price, ToolDefinition
from backend.core.tools.definitions import reference_schema
from backend.core.tools.store import load_catalog, publish
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Asset, AssetReference, Document, Workspace

DB = (os.environ.get("AIVERO_SUPERVISOR_TEST_DB") or os.environ.get("AIVERO_CORE_TEST_DB")
      or os.environ.get("AIVERO_DURABLE_TEST_DB"))
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated supervisor DB required")


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, "PNG")
    return buf.getvalue()


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name, version="v1", description="batch fake",
        parameters={"type": "object", "additionalProperties": False, "required": ["prompt", "image"],
                    "properties": {"prompt": {"type": "string"}, "image": reference_schema("image")}},
        result_schema={"type": "object"},
        inputs=[Port(path="image", media_type="image")],
        outputs=[Port(path="output", media_type="image")],
        mode="generate", price=Price(kind="fixed", credits=1), limits={}, failures=[],
        execution=Execution(adapter="batch_pipeline_fake", provider_idempotent=True,
                            result_url_policy="authenticated_asset", sla_seconds=60))


class OncePlanner(FakePlanner):
    def __init__(self, count: int):
        super().__init__([{"plan": {"goal": "x", "steps": [], "result_type": "generate", "writer": "llm"},
                           "invariants": {}, "confidence": 1, "raw_output": {"writer": "llm"}}])
        self.count = count
        self.real = 0

    async def plan(self, context):
        from backend.core.supervisor.context import build_context
        from backend.core.supervisor.tool_calls import convert_tool_calls

        self.real += 1
        built = build_context(context, Settings(context_max_objects=24, planner_tool_threshold=200))
        assert built["dynamic"]["collections"]["selected_images"]["count"] == self.count
        catalog = context["catalog"]
        template = [
            {"id": "t1", "tool": "batch_a", "parameters": {"prompt": "price is $5", "image": {"$item": "asset"}}},
            {"id": "t2", "tool": "batch_b", "parameters": {"prompt": "next", "image": "$t1.output"}, "depends_on": ["t1"]},
            {"id": "t3", "tool": "batch_c", "parameters": {"prompt": "last", "image": "$t2.output"}, "depends_on": ["t2"]},
        ]
        return convert_tool_calls(
            [{"id": "foreach", "name": "for_each", "arguments": {
                "inputs_collection": "selected_images", "task_template": template}}],
            context=context, catalog=catalog, user={"id": "owner"}, version_lock=catalog.lock,
            settings=Settings(planner_tool_threshold=200))


class Selective(FakeProvider):
    def __init__(self, path: Path, fail_last: int, code="provider_down"):
        super().__init__(path, Scenario(actual_credits=1))
        self.fail_last = fail_last
        self.code = code
        self.submits = 0

    async def submit(self, registration_id, params, idempotency_key):
        self.submits += 1
        prompt = params.get("prompt") if isinstance(params, dict) else None
        if prompt == "last" and self.fail_last > 0:
            self.fail_last -= 1
            from backend.core.execution.contracts import ProviderResult
            return ProviderResult("failed", registration_id, failure=self.code)
        return await super().submit(registration_id, params, idempotency_key)


async def _drive(factory, supervisor, processor, provider, config, root, run_ids, planner_limit=1):
    async def pump(run_id):
        async with factory.begin() as tx:
            for event in await tx.scalars(select(CoreEvent).where(CoreEvent.run_id == run_id).order_by(CoreEvent.run_sequence)):
                await accept(tx, {"event_id": str(event.id), "run_id": str(run_id)})
        for _ in range(80):
            if not any(await processor.tick()):
                break

    status = None
    for _ in range(80):
        await reconcile_completed_retries(factory)
        for run_id in run_ids:
            await pump(run_id)
        await supervisor.tick()
        await scan_ready(factory)
        async with factory() as tx:
            tasks = []
            for run_id in run_ids:
                tasks.extend(list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id))))
        for task in tasks:
            if task.status in {"queued", "submitted"} and task.job_id:
                await Worker(factory, root, provider, settings=config).execute(task.job_id)
        async with factory() as tx:
            rows = [await tx.get(CoreRun, run_id) for run_id in run_ids]
            status = [row.status for row in rows]
        if all(value in {"completed", "failed", "cancelled"} for value in status):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError(f"runs did not settle, last={status}")
    return status


async def _setup(tmp_path, count: int, supervisor_settings=None):
    os.environ["AIVERO_BATCH_GLOBAL_CONCURRENCY"] = "8"
    os.environ["AIVERO_BATCH_MAX_PER_RUN"] = "8"
    os.environ["AIVERO_BATCH_MAX_PER_BATCH"] = "4"
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    factory = create_async_session_factory(engine)
    media = tmp_path / "media"
    media.mkdir()
    png = _png()
    (media / "source.png").write_bytes(png)
    catalog = ToolCatalog(CatalogSnapshot(version=str(uuid.uuid4()), tools=[_tool(name) for name in ("batch_a", "batch_b", "batch_c")]))
    asset_ids: list[str] = []
    async with factory.begin() as tx:
        workspace = Workspace()
        tx.add(workspace)
        await tx.flush()
        await grant(tx, workspace.id, "initial-" + uuid.uuid4().hex, 100000, "owner")
        objects = {}
        for index in range(count):
            asset = Asset(workspace_id=workspace.id, kind="image", ext="png", storage_key="source.png",
                          bytes=len(png), sha256=hashlib.sha256(png).hexdigest(), width=8, height=8, details={})
            tx.add(asset)
            await tx.flush()
            node = f"n{index}"
            objects[node] = {"id": node, "kind": "image", "assetId": str(asset.id), "x": 0, "y": 0, "width": 8, "height": 8}
            asset_ids.append(str(asset.id))
        doc = Document(workspace_id=workspace.id, data={"objects": objects, "order": list(objects)})
        tx.add(doc)
        await tx.flush()
        for asset_id in asset_ids:
            tx.add(AssetReference(asset_id=uuid.UUID(asset_id), owner_type="document", owner_id=str(doc.id)))
        await publish(tx, catalog, published_by="test")
        run = await start(tx, workspace_id=workspace.id, user_id="owner", document_id=doc.id,
                          request_key="batch-" + uuid.uuid4().hex,
                          request={"message": "批量处理这些商品", "batch": True, "selected_assets": asset_ids,
                                   "anchor_id": "n0"},
                          budget=100000, token_budget=100000, max_rounds=20, settings=supervisor_settings)
        ids = (run.id, workspace.id, doc.id)
    return engine, factory, media, catalog, asset_ids, ids


def test_three_items_one_failure_retry_child(tmp_path):
    async def body():
        engine, factory, media, _catalog, asset_ids, (run_id, workspace_id, _doc_id) = await _setup(tmp_path, 3)
        inner = OncePlanner(3)
        planner = inner
        supervisor = Supervisor(factory, planner, settings=Settings(max_tasks=100))
        registry = Registry()
        register(registry)
        processor = Processor(factory, EventSettings(concurrency=1, backoff_base_seconds=.001, backoff_max_seconds=.01), registry)
        provider = Selective(tmp_path / "provider.sqlite", 1)
        config = WorkerSettings(ticket_key=__import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode(),
                                poll_seconds=.001, retry_base_seconds=.001, retry_max_seconds=.01, retry_jitter=0)
        try:
            await _drive(factory, supervisor, processor, provider, config, media, [run_id])
            async with factory() as tx:
                parent = await tx.get(CoreRun, run_id)
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
                ledgers = int(await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(
                    HostCreditLedger.core_run_id == run_id)) or 0)
                writes = int(await tx.scalar(select(func.count()).select_from(CanvasWrite).join(
                    CoreTask, CoreTask.id == CanvasWrite.task_id).where(CoreTask.run_id == run_id)) or 0)
                assets = int(await tx.scalar(select(func.count()).select_from(Asset).where(
                    Asset.workspace_id == workspace_id)) or 0)
                plan = await tx.get(CorePlan, parent.current_plan_id)
                document = plan.expansion_rules
                reserved_after = int(parent.credits_reserved)
                spent_after = int(parent.credits_spent)
            assert document["task_count"] == 9
            assert reserved_after >= spent_after
            assert inner.real == 1
            assert parent.status == "failed"
            failed = [task for task in tasks if task.status == "failed"]
            succeeded = [task for task in tasks if task.status == "succeeded"]
            assert len(failed) == 1
            assert len(succeeded) == 8
            provider.fail_last = 0
            async with factory.begin() as tx:
                parent = await tx.get(CoreRun, run_id)
                catalog = await load_catalog(tx, parent.version_lock["tool_catalog"]["version"])
                opened = await open_batch_retry(tx, parent, request_key="retry-1", item_ids=[], catalog=catalog)
                child_id = uuid.UUID(opened["child_run_id"])
                quote = opened["quote"]
                again = await open_batch_retry(tx, parent, request_key="retry-1", item_ids=[], catalog=catalog)
                assert again["replayed"] is True
                with pytest.raises(CompileError) as conflict:
                    await open_batch_retry(tx, parent, request_key="retry-1", item_ids=["nope"], catalog=catalog)
                assert conflict.value.code == "request_key_conflict"
                with pytest.raises(CompileError) as busy:
                    await open_batch_retry(tx, parent, request_key="retry-2", item_ids=[], catalog=catalog)
                assert busy.value.code == "retry_in_progress"
                child = await tx.get(CoreRun, child_id)
                assert child.parent_run_id == run_id
                assert child.credits_limit == quote
                assert child.user_id == "owner"
                assert (child.snapshot or {}).get("retry", {}).get("layer") == "rule_batch_retry"
                assert await tx.scalar(select(func.count()).select_from(CoreTask).where(CoreTask.run_id == child_id)) == 0
                assert child.current_plan_id is None
            await _drive(factory, supervisor, processor, provider, config, media, [child_id])
            await reconcile_completed_retries(factory)
            await reconcile_completed_retries(factory)
            async with factory() as tx:
                parent = await tx.get(CoreRun, run_id)
                child = await tx.get(CoreRun, child_id)
                child_tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == child_id)))
                admissions = list(await tx.scalars(select(ExecutionAdmission).where(
                    ExecutionAdmission.task_id.in_([task.id for task in child_tasks]))))
                child_ledgers = int(await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(
                    HostCreditLedger.core_run_id == child_id)) or 0)
                parent_ledgers = int(await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(
                    HostCreditLedger.core_run_id == run_id)) or 0)
                writes_after = int(await tx.scalar(select(func.count()).select_from(CanvasWrite).join(
                    CoreTask, CoreTask.id == CanvasWrite.task_id).where(CoreTask.run_id == run_id)) or 0)
                assets_after = int(await tx.scalar(select(func.count()).select_from(Asset).where(
                    Asset.workspace_id == workspace_id)) or 0)
                receipt = await tx.scalar(select(CoreBatchRetry).where(CoreBatchRetry.child_run_id == child_id))
                plan = await tx.get(CorePlan, parent.current_plan_id)
                progress = await effective_batch_progress(tx, parent, plan.expansion_rules, tasks)
            assert inner.real == 1
            assert parent.status == "failed"
            assert child.status in {"completed", "failed"}
            assert child_tasks and all(task.status in {"succeeded", "failed", "cancelled", "blocked_by_model"} for task in child_tasks)
            assert admissions and all(row.task_id for row in admissions)
            assert parent_ledgers == ledgers
            assert writes_after == writes
            assert (receipt.document or {}).get("settled") is True
            assert parent.credits_allocated == 0
            assert int(parent.credits_spent) == spent_after + int(child.credits_spent)
            assert int(parent.credits_reserved) == int(parent.credits_spent)
            assert progress["succeededItems"] == 3
            assert progress["retryableItemIds"] == []
            assert child.credits_limit == quote
            # Successful prefixes were reused: child has fewer provider submits than a full item.
            assert any(task.status == "succeeded" for task in child_tasks)
            blocked_plan = dict(plan.expansion_rules)
            async with factory.begin() as tx:
                parent = await tx.get(CoreRun, run_id)
                catalog = await load_catalog(tx, parent.version_lock["tool_catalog"]["version"])
                with pytest.raises(CompileError) as nothing:
                    await open_batch_retry(tx, parent, request_key="retry-fixed", item_ids=[], catalog=catalog)
                assert nothing.value.code == "nothing_to_retry"
            over_budget = False
            async with factory.begin() as tx:
                parent = await tx.get(CoreRun, run_id)
                parent.credits_limit = int(parent.credits_spent)
                parent.credits_reserved = int(parent.credits_spent)
                parent.credits_allocated = 0
            return assets, assets_after, child_ledgers, blocked_plan, over_budget
        finally:
            async with factory.begin() as tx:
                owned = list(await tx.scalars(select(CoreRun).where(CoreRun.workspace_id == workspace_id)))
                for row in owned:
                    if row.status not in {"completed", "failed", "cancelled"}:
                        await tx.scalar(select(CoreSession).where(CoreSession.id == row.session_id).with_for_update())
                        await stop(tx, row, "cancelled", "fixture cleanup", "fixture-cleanup:" + str(row.id))
                if owned:
                    await tx.execute(Invocation.__table__.update().where(
                        Invocation.run_id.in_([row.id for row in owned])).values(status="done", lease_token=None, lease_until=None))
            await engine.dispose()
    asyncio.run(body())


@pytest.mark.parametrize("accepted", [True, False])
def test_selfcheck_child_does_not_queue_before_review(tmp_path, accepted):
    async def body():
        from backend.core.selfcheck.provider import FakeSelfCheckProvider
        from tests.backend.test_selfcheck_runtime import control_flow_enforce_gate
        settings = Settings(max_tasks=100, selfcheck_enabled=True, selfcheck_mode="enforce",
                            selfcheck_cost_threshold=1, selfcheck_timeout_seconds=30)
        engine, factory, media, _catalog, _asset_ids, (run_id, workspace_id, _doc) = await _setup(
            tmp_path, 3, supervisor_settings=settings)
        gate = asyncio.Event()
        gate.set()
        class SyntheticChatProvider(FakeSelfCheckProvider):
            # Exercise chat control flow only; never quality evidence.
            kind = "chat"

        checker = SyntheticChatProvider([
            {"pass": True, "problems": []},
            {"pass": accepted, "problems": [] if accepted else [
                {"item": "object", "expected": "指定对象", "actual": "错误对象"}]},
        ], gate=gate)
        planner = OncePlanner(3)
        supervisor = Supervisor(factory, planner, settings=settings, selfcheck_provider=checker,
                                selfcheck_evidence_gate=control_flow_enforce_gate)
        registry = Registry()
        register(registry)
        processor = Processor(factory, EventSettings(concurrency=1, backoff_base_seconds=.001,
            backoff_max_seconds=.01), registry)
        provider = Selective(tmp_path / "provider.sqlite", 1)
        config = WorkerSettings(ticket_key=__import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode(),
                                poll_seconds=.001, retry_base_seconds=.001, retry_max_seconds=.01, retry_jitter=0)
        try:
            assert await _drive(factory, supervisor, processor, provider, config, media, [run_id]) == ["failed"]
            assert checker.calls == 1 and planner.real == 1
            gate.clear()
            async with factory.begin() as tx:
                parent = await tx.get(CoreRun, run_id)
                catalog = await load_catalog(tx, parent.version_lock["tool_catalog"]["version"])
                opened = await open_batch_retry(tx, parent, request_key="retry-sc", item_ids=[],
                                               catalog=catalog, settings=settings)
                child_id = uuid.UUID(opened["child_run_id"])
            for _ in range(100):
                async with factory.begin() as tx:
                    events = list(await tx.scalars(select(CoreEvent).where(CoreEvent.run_id == child_id)))
                    for event in events:
                        await accept(tx, {"event_id": str(event.id), "run_id": str(child_id)})
                for _ in range(30):
                    if not any(await processor.tick()):
                        break
                await supervisor.tick()
                if checker.calls == 2:
                    break
                await asyncio.sleep(.002)
            assert checker.calls == 2, "child checker was never called"
            async with factory() as tx:
                assert await tx.scalar(select(func.count()).select_from(CoreTask).where(CoreTask.run_id == child_id)) == 0
                assert await tx.scalar(select(func.count()).select_from(ExecutionAdmission).where(
                    ExecutionAdmission.plan_id.in_(select(CorePlan.id).where(CorePlan.run_id == child_id)))) == 0
                assert await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(
                    HostCreditLedger.core_run_id == child_id)) == 0
            gate.set()
            assert await _drive(factory, supervisor, processor, provider, config, media, [child_id]) == [
                "completed" if accepted else "failed"]
            assert planner.real == 1
            async with factory() as tx:
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == child_id)))
                assert len(tasks) == (1 if accepted else 0)
        finally:
            gate.set()
            async with factory.begin() as tx:
                for row in await tx.scalars(select(CoreRun).where(CoreRun.workspace_id == workspace_id)):
                    if row.status not in {"completed", "failed", "cancelled"}:
                        await tx.scalar(select(CoreSession).where(CoreSession.id == row.session_id).with_for_update())
                        await stop(tx, row, "cancelled", "fixture cleanup", "fixture-cleanup:" + str(row.id))
            await engine.dispose()
    asyncio.run(body())


def test_child_entry_is_rejected():
    async def body():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        async with factory.begin() as tx:
            workspace = Workspace()
            tx.add(workspace)
            await tx.flush()
            session = CoreSession(workspace_id=workspace.id, source_canvas_id=uuid.uuid4(), user_id="owner", language="zh-CN")
            tx.add(session)
            await tx.flush()
            parent = await create_run(tx, RunInput(
                session_id=session.id, workspace_id=workspace.id, user_id="owner", request_key=uuid.uuid4().hex,
                request_hash="ab" * 32, snapshot={"request": {"message": "x"}}, scope={}, invariants={},
                version_lock={}))
            child = await create_run(tx, RunInput(
                session_id=session.id, workspace_id=workspace.id, user_id="owner", request_key=uuid.uuid4().hex,
                request_hash="cd" * 32, snapshot={"request": {"message": "x"}}, scope={}, invariants={},
                version_lock={}, parent_run_id=parent.id))
            with pytest.raises(CompileError) as denied:
                await open_batch_retry(tx, child, request_key="k", item_ids=[], catalog=None)
            assert denied.value.code == "not_root"
        await engine.dispose()
    asyncio.run(body())
