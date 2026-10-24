"""Batch cutout, poster text, and three sizes on an isolated migrated database.

No rembg, no model API, and no credential reads. Workers run the real phase machine.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import uuid

import pytest
from cryptography.fernet import Fernet
from PIL import Image
from sqlalchemy import func, select, update

from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.agent.media.credits import grant
from backend.agent.orm import HostCreditLedger
from backend.core.batch.orm import CoreBatchRetry
from backend.core.batch.retry_helpers import effective_batch_progress
from backend.core.batch.service import open_batch_retry
from backend.core.events.contracts import EventSettings
from backend.core.events.processor import Processor
from backend.core.events.registry import Registry
from backend.core.events.store import accept
from backend.core.execution.contracts import ProviderResult, WorkerSettings
from backend.core.execution.engine import Worker, scan_ready
from backend.core.execution.local import LocalAdapter
from backend.core.execution.orm import CanvasWrite
from backend.core.orm import CoreAssetVersion, CoreEvent, CorePlan, CoreRun, CoreSession, CoreSessionMessage, CoreTask
from backend.core.supervisor.contracts import Settings
from backend.core.supervisor.engine import Supervisor, register, stop
from backend.core.supervisor.orm import Invocation
from backend.core.supervisor.service import start
from backend.core.supervisor.tool_calls import convert_tool_calls
from backend.core.tools import build_catalog, ToolSettings
from backend.core.tools.store import load_catalog, publish
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Asset, AssetReference, Document, Workspace
from test_supervisor_pipeline import DetectFake

DB = (os.environ.get("AIVERO_SUPERVISOR_TEST_DB") or os.environ.get("AIVERO_CORE_TEST_DB")
      or os.environ.get("AIVERO_DURABLE_TEST_DB"))
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated supervisor DB required")

COPY = "月圆人团圆"


def _png(index: int) -> bytes:
    image = Image.new("RGBA", (8, 8), (index * 20 % 255, 40, 90, 255))
    for x in range(4):
        for y in range(8):
            image.putpixel((x, y), (220, 20, 20, 255))
    raw = io.BytesIO()
    image.save(raw, "PNG")
    return raw.getvalue()


class RoutingLocal(LocalAdapter):
    """Real local detect/cutout/compose. Selected compose task ids fail once."""

    def __init__(self, tool, factory, root, task_id, fail_ids):
        super().__init__(tool, factory, root)
        self.labels = ["主体"]
        self.task_id = str(task_id)
        self.fail_ids = fail_ids

    async def compute(self, registration_id, params):
        if self.tool.name == "compose_layers" and self.task_id in self.fail_ids:
            return ProviderResult("failed", registration_id, failure="provider_down")
        if self.tool.name == "detect_subject":
            return await DetectFake.compute(self, registration_id, params)
        return await super().compute(registration_id, params)


class CountingPlanner:
    def __init__(self, kind: str, sizes=None):
        self.kind = kind
        self.sizes = sizes
        self.calls = 0

    async def plan(self, context):
        self.calls += 1
        catalog = context["catalog"]
        if self.kind == "cutout":
            arguments = {"inputs_collection": "selected_images", "task_template": [
                {"id": "t1", "tool": "detect_subject", "parameters": {"image": {"$item": "asset"}}},
                {"id": "t2", "tool": "cutout", "parameters": {"image": {"$item": "asset"}, "mask": "$t1.output"},
                 "depends_on": ["t1"]},
                {"id": "t3", "tool": "compose_layers", "parameters": {
                    "background": {"kind": "color", "color": "#FFFFFF"}, "subject": "$t2.output",
                    "text_layers": [], "fit": "contain", "preset": "feed_square"},
                 "depends_on": ["t2"]},
            ]}
            if self.sizes:
                arguments["sizes"] = [{"id": item} if isinstance(item, str) else item for item in self.sizes]
                arguments["task_template"][2]["parameters"] = {
                    "background": {"kind": "color", "color": "#FFFFFF"}, "subject": "$t2.output",
                    "text_layers": [], "fit": "contain", "preset": {"$size": "id"}}
            calls = [{"id": "foreach", "name": "for_each", "arguments": arguments}]
        else:
            calls = [
                {"id": "c1", "name": "text_layout", "arguments": {
                    "copy": COPY, "font_id": "noto-sans-sc", "size": 64, "color": "#112233",
                    "alignment": "center", "region": "title", "preset": "feed_square"}},
                {"id": "c2", "name": "compose_layers", "arguments": {
                    "background": {"kind": "color", "color": "#FFFFFF"}, "text_layers": ["$t1.output"],
                    "fit": "contain", "preset": "feed_square", "_plan": {"depends_on": ["t1"]}}},
            ]
        return convert_tool_calls(calls, context=context, catalog=catalog, user={"id": "owner"},
                                  version_lock=catalog.lock, settings=Settings(planner_tool_threshold=200))


async def _setup(tmp_path, count: int):
    os.environ["AIVERO_BATCH_GLOBAL_CONCURRENCY"] = "8"
    os.environ["AIVERO_BATCH_MAX_PER_RUN"] = "8"
    os.environ["AIVERO_BATCH_MAX_PER_BATCH"] = "4"
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    factory = create_async_session_factory(engine)
    root = tmp_path / "media"
    root.mkdir()
    version = uuid.uuid4().hex
    catalog = build_catalog(Catalog(CatalogConfig(version=version, environment="development",
                                                  enabled=True, capabilities=[])), version,
                            ToolSettings(cutout_credits=2, compose_credits=1, text_layout_credits=1))
    for name in ("detect_subject", "cutout", "text_layout", "compose_layers"):
        tool = catalog.get_tool(name, catalog.lock.version)
        assert tool.available, name
    asset_ids = []
    async with factory.begin() as tx:
        workspace = Workspace()
        tx.add(workspace)
        await tx.flush()
        await grant(tx, workspace.id, "grant-" + uuid.uuid4().hex, 100000, "owner")
        objects = {}
        for index in range(count):
            blob = _png(index)
            key = f"sources/{uuid.uuid4().hex}.png"
            (root / "sources").mkdir(exist_ok=True)
            (root / key).write_bytes(blob)
            asset = Asset(workspace_id=workspace.id, kind="image", ext="png", storage_key=key,
                          bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest(), width=8, height=8, details={})
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
        await publish(tx, catalog, published_by="batch-composition")
        run = await start(tx, workspace_id=workspace.id, user_id="owner", document_id=doc.id,
                          request_key="batch-" + uuid.uuid4().hex,
                          request={"message": "全部抠图换白底", "batch": True, "selected_assets": asset_ids, "anchor_id": "n0"},
                          budget=100000, token_budget=100000, max_rounds=20)
        ids = (run.id, workspace.id, doc.id)
    config = WorkerSettings(ticket_key=Fernet.generate_key().decode(), poll_seconds=.001,
                            retry_base_seconds=.001, retry_max_seconds=.01, retry_jitter=0, call_timeout_seconds=30)
    return engine, factory, root, catalog, ids, config


def _wire(factory, planner):
    supervisor = Supervisor(factory, planner, settings=Settings(max_tasks=100))
    registry = Registry()
    register(registry)
    processor = Processor(factory, EventSettings(concurrency=1, backoff_base_seconds=.001, backoff_max_seconds=.01), registry)
    return supervisor, processor


async def _drive(factory, supervisor, processor, root, config, catalog, run_ids, *, fail_compose=0):
    seen = set()
    fail_ids = set()

    async def pump(run_id):
        async with factory.begin() as tx:
            rows = list(await tx.scalars(select(CoreEvent).where(CoreEvent.run_id == run_id).order_by(CoreEvent.run_sequence)))
        fresh = [row for row in rows if row.id not in seen]
        async with factory.begin() as tx:
            for row in fresh:
                seen.add(row.id)
                await accept(tx, {"event_id": str(row.id), "run_id": str(run_id)})
        for _ in range(40):
            if not any(await processor.tick()):
                break

    status = None
    for _ in range(800):
        for run_id in run_ids:
            await pump(run_id)
        await supervisor.tick()
        await scan_ready(factory)
        async with factory() as tx:
            tasks = []
            for run_id in run_ids:
                tasks.extend(list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id))))
        if fail_compose and not fail_ids:
            compose_ids = [task.id for task in tasks if task.tool_ref == "compose_layers"]
            if len(compose_ids) >= fail_compose:
                fail_ids = {str(item) for item in compose_ids[:fail_compose]}
        for task in tasks:
            if task.status in {"queued", "submitted"} and task.job_id:
                tool = catalog.get_tool(task.tool_ref, catalog.lock.version)
                provider = RoutingLocal(tool, factory, root, task.id, fail_ids)
                await Worker(factory, root, provider, settings=config).execute(task.job_id)
        async with factory() as tx:
            rows = [await tx.get(CoreRun, run_id) for run_id in run_ids]
            status = [row.status for row in rows]
        if all(value in {"completed", "failed", "cancelled"} for value in status):
            break
        await asyncio.sleep(0)
    else:
        async with factory() as tx:
            detail = []
            for run_id in run_ids:
                row = await tx.get(CoreRun, run_id)
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
                invocations = list(await tx.scalars(select(Invocation).where(Invocation.run_id == run_id)))
            counts = {}
            for task in tasks:
                counts[task.status] = counts.get(task.status, 0) + 1
            detail.append({"status": row.status, "pause": row.pause_reason,
                    "planner_calls": getattr(supervisor.planner, "calls", None),
                    "task_counts": counts, "task_total": len(tasks),
                    "reviews": [(item.review or {}).get("reasons") for item in invocations]})
        raise AssertionError(f"runs did not settle, last={status}, detail={detail}")
    for _ in range(3):
        async with factory() as tx:
            replay = list(await tx.scalars(select(CoreEvent).where(CoreEvent.run_id.in_(run_ids))))
        async with factory.begin() as tx:
            for row in replay:
                await accept(tx, {"event_id": str(row.id), "run_id": str(row.run_id)})
        for _ in range(10):
            if not any(await processor.tick()):
                break
    return fail_ids


async def _cleanup(factory, engine, workspace_id):
    async with factory.begin() as tx:
        owned = list(await tx.scalars(select(CoreRun).where(CoreRun.workspace_id == workspace_id)))
        for row in owned:
            if row.status not in {"completed", "failed", "cancelled"}:
                await tx.scalar(select(CoreSession).where(CoreSession.id == row.session_id).with_for_update())
                await stop(tx, row, "cancelled", "fixture cleanup", "fixture-cleanup:" + str(row.id))
        await tx.execute(update(Invocation).where(Invocation.run_id.in_([row.id for row in owned]))
                         .values(status="done", lease_token=None, lease_until=None))
        sessions = select(CoreSession.id).where(CoreSession.workspace_id == workspace_id)
        await tx.execute(update(CoreSessionMessage).where(CoreSessionMessage.session_id.in_(sessions),
            CoreSessionMessage.processing_status == "pending").values(processing_status="cancelled"))
        await tx.execute(update(CoreSession).where(CoreSession.workspace_id == workspace_id).values(
            closed_at=func.now(), dispatch_at=None, dispatch_lease_owner=None, dispatch_lease_until=None))
    await engine.dispose()


def test_three_cutouts_then_one_compose_retry(tmp_path):
    async def body():
        engine, factory, root, catalog, (run_id, workspace_id, doc_id), config = await _setup(tmp_path, 3)
        planner = CountingPlanner("cutout")
        supervisor, processor = _wire(factory, planner)
        try:
            await _drive(factory, supervisor, processor, root, config, catalog, [run_id], fail_compose=1)
            async with factory() as tx:
                parent = await tx.get(CoreRun, run_id)
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
                parent_writes = int(await tx.scalar(select(func.count()).select_from(CanvasWrite).join(
                    CoreTask, CoreTask.id == CanvasWrite.task_id).where(CoreTask.run_id == run_id)) or 0)
                groups = [task for task in tasks if task.tool_ref == "compose_layers" and task.status == "succeeded"]
            assert planner.calls == 1
            assert parent.status == "failed"
            assert len([task for task in tasks if task.status == "failed" and task.tool_ref == "compose_layers"]) == 1
            assert len(groups) == 2
            async with factory.begin() as tx:
                parent = await tx.get(CoreRun, run_id)
                loaded = await load_catalog(tx, parent.version_lock["tool_catalog"]["version"])
                opened = await open_batch_retry(tx, parent, request_key="retry-cutout", item_ids=[], catalog=loaded)
                child_id = uuid.UUID(opened["child_run_id"])
            await _drive(factory, supervisor, processor, root, config, catalog, [child_id], fail_compose=0)
            from backend.core.batch.retry_helpers import reconcile_completed_retries
            await reconcile_completed_retries(factory)
            await reconcile_completed_retries(factory)
            async with factory() as tx:
                parent = await tx.get(CoreRun, run_id)
                plan = await tx.get(CorePlan, parent.current_plan_id)
                root_tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
                progress = await effective_batch_progress(tx, parent, plan.expansion_rules, root_tasks)
                child_tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == child_id)))
                writes_after = int(await tx.scalar(select(func.count()).select_from(CanvasWrite).join(
                    CoreTask, CoreTask.id == CanvasWrite.task_id).where(CoreTask.run_id == run_id)) or 0)
            assert planner.calls == 1
            assert parent.status == "failed"
            assert progress["retryableItemIds"] == []
            assert writes_after == parent_writes
            assert child_tasks and all(task.tool_ref == "compose_layers" for task in child_tasks)
            assert all(task.status == "succeeded" for task in child_tasks)
        finally:
            await _cleanup(factory, engine, workspace_id)
    asyncio.run(body())


def test_hundred_cutouts_retry_five_compose_failures(tmp_path):
    async def body():
        engine, factory, root, catalog, (run_id, workspace_id, _doc_id), config = await _setup(tmp_path, 100)
        planner = CountingPlanner("cutout")
        supervisor, processor = _wire(factory, planner)
        try:
            await _drive(factory, supervisor, processor, root, config, catalog, [run_id], fail_compose=5)
            async with factory() as tx:
                parent = await tx.get(CoreRun, run_id)
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
                parent_writes = int(await tx.scalar(select(func.count()).select_from(CanvasWrite).join(
                    CoreTask, CoreTask.id == CanvasWrite.task_id).where(CoreTask.run_id == run_id)) or 0)
                parent_ledgers = int(await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(
                    HostCreditLedger.core_run_id == run_id)) or 0)
            assert planner.calls == 1
            assert parent.status == "failed"
            assert len([task for task in tasks if task.status == "failed"]) == 5
            async with factory.begin() as tx:
                parent = await tx.get(CoreRun, run_id)
                loaded = await load_catalog(tx, parent.version_lock["tool_catalog"]["version"])
                opened = await open_batch_retry(tx, parent, request_key="retry-100", item_ids=[], catalog=loaded)
                child_id = uuid.UUID(opened["child_run_id"])
            await _drive(factory, supervisor, processor, root, config, catalog, [child_id])
            from backend.core.batch.retry_helpers import reconcile_completed_retries
            await reconcile_completed_retries(factory)
            await reconcile_completed_retries(factory)
            async with factory() as tx:
                parent = await tx.get(CoreRun, run_id)
                plan = await tx.get(CorePlan, parent.current_plan_id)
                root_tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
                progress = await effective_batch_progress(tx, parent, plan.expansion_rules, root_tasks)
                child_tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == child_id)))
                writes_after = int(await tx.scalar(select(func.count()).select_from(CanvasWrite).join(
                    CoreTask, CoreTask.id == CanvasWrite.task_id).where(CoreTask.run_id == run_id)) or 0)
                ledgers_after = int(await tx.scalar(select(func.count()).select_from(HostCreditLedger).where(
                    HostCreditLedger.core_run_id == run_id)) or 0)
                receipt = await tx.scalar(select(CoreBatchRetry).where(CoreBatchRetry.child_run_id == child_id))
                doc = await tx.get(Document, _doc_id)
                final_writes = list(await tx.scalars(select(CanvasWrite).join(CoreTask, CoreTask.id == CanvasWrite.task_id)
                    .where(CoreTask.run_id.in_([run_id, child_id]), CanvasWrite.history_only.is_(False))))
                ledgers = list(await tx.scalars(select(HostCreditLedger).where(
                    HostCreditLedger.core_run_id.in_([run_id, child_id]))))
                from backend.core.orm import CoreAssetVersion
                versions = list(await tx.scalars(select(CoreAssetVersion).where(
                    CoreAssetVersion.run_id.in_([run_id, child_id]))))
            assert len(final_writes) == 100
            assert len([obj for obj in doc.data["objects"].values() if obj.get("layerRole") == "group"]) == 100
            assert all(row.node_id for row in final_writes)
            assert sum(row.charged for row in ledgers) == 300
            assert sum(row.refunded for row in ledgers) == 5
            assert all(row.quoted == row.charged + row.refunded for row in ledgers)
            assert parent.credits_spent == 300 and parent.credits_allocated == 0
            assert versions and all(row.task_id is not None and row.user_id == "owner" for row in versions)
            assert planner.calls == 1
            assert parent.status == "failed"
            assert parent.max_thought_rounds == 20
            assert progress.get("retryableItemIds") == []
            assert progress.get("succeededItems") == 100 or progress.get("completedItems") == 100
            assert writes_after == parent_writes
            assert ledgers_after == parent_ledgers
            assert len(child_tasks) == 5
            assert all(task.status == "succeeded" and task.tool_ref == "compose_layers" for task in child_tasks)
            assert (receipt.document or {}).get("settled") is True
        finally:
            await _cleanup(factory, engine, workspace_id)
    asyncio.run(body())


def test_poster_text_is_exact(tmp_path):
    async def body():
        engine, factory, root, catalog, (run_id, workspace_id, doc_id), config = await _setup(tmp_path, 1)
        planner = CountingPlanner("poster")
        supervisor, processor = _wire(factory, planner)
        try:
            await _drive(factory, supervisor, processor, root, config, catalog, [run_id])
            async with factory() as tx:
                parent = await tx.get(CoreRun, run_id)
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id)))
                versions = list(await tx.scalars(select(CoreAssetVersion).where(CoreAssetVersion.run_id == run_id)))
                doc = await tx.get(Document, doc_id)
            assert planner.calls == 1
            assert parent.status == "completed"
            assert {task.tool_ref for task in tasks} <= {"text_layout", "compose_layers"}
            blobs = []
            for version in versions:
                layers = version.layers or {}
                blobs.append(str(layers))
            assert any(COPY in blob for blob in blobs)
            assert COPY in str(doc.data)
        finally:
            await _cleanup(factory, engine, workspace_id)
    asyncio.run(body())


def test_one_subject_three_preset_sizes(tmp_path):
    async def body():
        engine, factory, root, catalog, (run_id, workspace_id, doc_id), config = await _setup(tmp_path, 1)
        planner = CountingPlanner("cutout", sizes=["feed_square", "story_portrait", "banner_landscape"])
        supervisor, processor = _wire(factory, planner)
        try:
            await _drive(factory, supervisor, processor, root, config, catalog, [run_id])
            async with factory() as tx:
                parent = await tx.get(CoreRun, run_id)
                tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run_id, CoreTask.tool_ref == "compose_layers")))
                versions = list(await tx.scalars(select(CoreAssetVersion).where(CoreAssetVersion.run_id == run_id)))
            assert parent.status == "completed"
            assert len(tasks) == 3
            assert {(version.width, version.height) for version in versions if version.kind == "image" or version.width} >= {
                (1080, 1080), (1080, 1920), (1920, 1080)}
        finally:
            await _cleanup(factory, engine, workspace_id)
    asyncio.run(body())
