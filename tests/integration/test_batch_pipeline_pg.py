"""PostgreSQL batch pipeline: one planner call, real supervisor advance, fake provider."""
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
from backend.core.batch.contracts import BatchSettings
from backend.core.batch.orm import CoreBatchDispatch, CoreBatchQuota
from backend.core.batch.store import GLOBAL_QUOTA_KEY, claim_dispatch, settle_dispatch
from backend.core.contracts import RunInput
from backend.core.events.contracts import EventSettings
from backend.core.events.processor import Processor
from backend.core.events.registry import Registry
from backend.core.events.store import accept
from backend.core.execution.contracts import WorkerSettings
from backend.core.execution.engine import Worker, scan_ready
from backend.core.execution.fake import FakeProvider, Scenario
from backend.core.orm import CoreEvent, CoreRun, CoreSession, CoreTask
from backend.core.store import create_run
from backend.core.supervisor.contracts import Settings
from backend.core.supervisor.engine import Supervisor, register, stop
from backend.core.supervisor.orm import Invocation
from backend.core.supervisor.planners import FakePlanner
from backend.core.supervisor.service import start
from backend.core.tools import ToolCatalog
from backend.core.tools.contracts import CatalogSnapshot, Execution, Port, Price, ToolDefinition
from backend.core.tools.definitions import reference_schema
from backend.core.tools.store import publish
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Asset, AssetReference, Document, Workspace
from backend.agent.orm import HostCreditLedger

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
        assert built["dynamic"]["canvas"]["selected"] == []
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
    def __init__(self, path: Path, fail_assets: set[str]):
        super().__init__(path, Scenario(actual_credits=1))
        self.fail_assets = fail_assets
        self.submits = 0

    async def submit(self, registration_id, params, idempotency_key):
        self.submits += 1
        image = params.get("image") if isinstance(params, dict) else None
        asset_id = image.get("asset_id") if isinstance(image, dict) else None
        if asset_id in self.fail_assets:
            from backend.core.execution.contracts import ProviderResult
            return ProviderResult("failed", registration_id, failure="param_invalid")
        return await super().submit(registration_id, params, idempotency_key)


async def _run_case(tmp_path, count: int, failures: int):
    os.environ["AIVERO_BATCH_GLOBAL_CONCURRENCY"] = "8"
    os.environ["AIVERO_BATCH_MAX_PER_RUN"] = "8"
    os.environ["AIVERO_BATCH_MAX_PER_BATCH"] = "8"
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    factory = create_async_session_factory(engine)
    root = tmp_path / "media"
    root.mkdir()
    png = _png()
    (root / "source.png").write_bytes(png)
    catalog = ToolCatalog(CatalogSnapshot(version=str(uuid.uuid4()), tools=[_tool(name) for name in ("batch_a", "batch_b", "batch_c")]))
    asset_ids: list[str] = []
    async with factory.begin() as tx:
        workspace = Workspace()
        tx.add(workspace)
        await tx.flush()
        await grant(tx, workspace.id, "initial", 100000, "owner")
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
        run = await start(tx, workspace_id=workspace.id, user_id="owner", document_id=doc.id, request_key="batch",
                          request={"message": "全部抠图换白底", "selected_assets": asset_ids,
                                   "anchor_id": "n0"},
                          budget=100000, token_budget=100000, max_rounds=20)
        run_id, workspace_id = run.id, workspace.id
    planner = OncePlanner(count)
    supervisor = Supervisor(factory, planner, settings=Settings(max_tasks=100))
    registry = Registry()
    register(registry)
    processor = Processor(factory, EventSettings(concurrency=1, backoff_base_seconds=.001, backoff_max_seconds=.01), registry)
    provider = Selective(tmp_path / "provider.sqlite", set(asset_ids[:failures]))
    config = WorkerSettings(ticket_key=__import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode(),
                            poll_seconds=.001, retry_base_seconds=.001, retry_max_seconds=.01, retry_jitter=0)

    seen: set = set()

    async def pump(*, replay: bool = False):
        async with factory.begin() as tx:
            events = list(await tx.scalars(select(CoreEvent).where(CoreEvent.run_id == run_id).order_by(CoreEvent.run_sequence)))
            for event in events:
                if replay or event.id not in seen:
                    await accept(tx, {"event_id": str(event.id), "run_id": str(run_id)})
                    seen.add(event.id)
        for _ in range(300):
            if not any(await processor.tick()):
                break

    try:
        status = None
        for _ in range(max(40, count * 3)):
            await pump()
            await supervisor.tick()
            await scan_ready(factory)
            async with factory() as tx:
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
            for task in tasks:
                if task.status in {"queued", "submitted"} and task.job_id:
                    await Worker(factory, root, provider, settings=config).execute(task.job_id)
            async with factory() as tx:
                row = await tx.get(CoreRun, run_id)
                status = row.status
            if status in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError(f"run did not settle, last={status}")
        submits = provider.submits
        async with factory() as tx:
            ledgers = int(await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(
                HostCreditLedger.core_run_id == run_id)) or 0)
        for _ in range(3):
            await pump(replay=True)
            await supervisor.tick()
            await scan_ready(factory)
        async with factory() as tx:
            run = await tx.get(CoreRun, run_id)
            tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
            dispatches = list(await tx.scalars(select(CoreBatchDispatch).where(CoreBatchDispatch.run_id == run_id)))
            quota = await tx.scalar(select(CoreBatchQuota).where(CoreBatchQuota.quota_key == GLOBAL_QUOTA_KEY))
            ledgers_again = int(await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(
                HostCreditLedger.core_run_id == run_id)) or 0)
        assert planner.real == 1
        assert run.version_lock["prompt_version"] == "supervisor-v2"
        assert run.version_lock["planner_controls"]["version"] == "planner-controls-v2"
        assert (run.snapshot or {})["request"]["batch"] is True
        assert run.max_thought_rounds == 20
        assert run.thought_rounds_used == 1
        assert run.status == "failed"
        assert all(task.status in {"succeeded", "failed", "cancelled", "blocked_by_model", "rejected"} for task in tasks)
        assert len(tasks) == count * 3
        assert all(task.task_key[:1] == "t" and ":" not in task.task_key for task in tasks)
        failed = [task for task in tasks if task.status == "failed"]
        cancelled = [task for task in tasks if task.status == "cancelled"]
        succeeded = [task for task in tasks if task.status == "succeeded"]
        assert len(failed) == failures
        assert len(cancelled) == failures * 2
        assert len(succeeded) == (count - failures) * 3
        assert all((task.error or {}).get("code") == "upstream_failed" for task in cancelled)
        assert quota is not None and quota.inflight == 0
        assert dispatches and all(row.state == "settled" for row in dispatches)
        assert provider.submits == submits
        assert ledgers_again == ledgers
        assert ledgers == len(succeeded) + len(failed)
        return status
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


def test_three_items_isolate_one_failure(tmp_path):
    asyncio.run(_run_case(tmp_path, 3, 1))


def test_hundred_items_five_failures_release_slots(tmp_path):
    asyncio.run(_run_case(tmp_path, 100, 5))


def test_global_cap_is_shared_and_unknown_keys_do_not_release(tmp_path):
    async def test():
        os.environ["AIVERO_BATCH_GLOBAL_CONCURRENCY"] = "8"
        os.environ["AIVERO_BATCH_MAX_PER_RUN"] = "4"
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        settings = BatchSettings(global_concurrency=8, max_per_run=4)
        async with factory.begin() as tx:
            first, second = Workspace(), Workspace()
            tx.add_all([first, second])
            await tx.flush()
            runs = []
            for workspace in (first, second):
                session = CoreSession(workspace_id=workspace.id, source_canvas_id=uuid.uuid4(), user_id="owner", language="zh-CN")
                tx.add(session)
                await tx.flush()
                run = await create_run(tx, RunInput(
                    session_id=session.id, workspace_id=workspace.id, user_id="owner", request_key=uuid.uuid4().hex,
                    request_hash="cd" * 32, snapshot={}, scope={}, invariants={}, version_lock={}))
                runs.append(run.id)
        try:
            async with factory.begin() as tx:
                quota = await tx.scalar(select(CoreBatchQuota).where(CoreBatchQuota.quota_key == GLOBAL_QUOTA_KEY))
                if quota is None:
                    tx.add(CoreBatchQuota(quota_key=GLOBAL_QUOTA_KEY, workspace_id=None, inflight=0, limit_value=1))
                else:
                    assert quota.inflight == 0
                    quota.limit_value = 1
            async with factory.begin() as tx:
                assert await claim_dispatch(tx, workspace_id=first.id, run_id=runs[0], task_key="t1",
                                            credits=1, budget_left=10, settings=settings) == "reserved"
                stored = await tx.scalar(select(CoreBatchQuota).where(CoreBatchQuota.quota_key == GLOBAL_QUOTA_KEY))
                assert stored.limit_value == 1
            async with factory.begin() as tx:
                assert await claim_dispatch(tx, workspace_id=second.id, run_id=runs[1], task_key="t1",
                                            credits=1, budget_left=10, settings=settings) == "concurrency_rejected"
                assert await settle_dispatch(tx, runs[1], "t9") == "not_submitted"
            async with factory.begin() as tx:
                quota = await tx.scalar(select(CoreBatchQuota).where(CoreBatchQuota.quota_key == GLOBAL_QUOTA_KEY))
                assert quota.inflight == 1
        finally:
            async with factory.begin() as tx:
                from backend.core.batch.store import ack_dispatch
                await ack_dispatch(tx, runs[0], "t1")
                await settle_dispatch(tx, runs[0], "t1")
            await engine.dispose()
    asyncio.run(test())
