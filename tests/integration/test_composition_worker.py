"""Text layout then compose on isolated PG. No provider generation."""

import asyncio
import hashlib
import io
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from PIL import Image
from sqlalchemy import func, select, update

from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.agent.media.credits import grant
from backend.agent.orm import HostCreditLedger
from backend.core.contracts import CoreSettings, RunInput, TaskInput
from backend.core.execution.contracts import WorkerSettings
from backend.core.execution.engine import enqueue, record_admission
from backend.core.execution.orm import CanvasWrite, TaskExecution
from backend.core.orm import CoreAssetLineage, CoreAssetVersion, CorePlan, CoreSession, CoreTask, CoreTaskDependency
from backend.core.store import create_run, create_task, transition
from backend.core.tools import build_catalog
from backend.core.tools.contracts import ToolSettings
from backend.core.tools.store import publish
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Asset, AssetReference, Document, Job, Workspace

DB = (os.environ.get("AIVERO_COMPOSITION_TEST_DB") or os.environ.get("AIVERO_CORE_TEST_DB")
      or os.environ.get("AIVERO_EXECUTION_TEST_DB"))
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated composition PG required")


def _settings():
    return WorkerSettings(ticket_key=Fernet.generate_key().decode(), lease_seconds=2,
        call_timeout_seconds=30, poll_seconds=0.001, retry_base_seconds=0.001,
        retry_max_seconds=0.01, retry_jitter=0)


def _subject() -> bytes:
    image = Image.new("RGBA", (32, 32), (200, 10, 10, 255))
    image.putpixel((0, 0), (0, 0, 0, 0))
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    return raw.getvalue()


@asynccontextmanager
async def opened(tmp_path: Path, tool_settings=None):
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    factory = create_async_session_factory(engine)
    root = tmp_path / "media"
    root.mkdir()
    config = _settings()
    version = uuid.uuid4().hex
    catalog = build_catalog(Catalog(CatalogConfig(version=version, environment="development",
        enabled=True, capabilities=[])), version, tool_settings)
    async with factory.begin() as tx:
        workspace = uuid.uuid4()
        tx.add(Workspace(id=workspace))
        await tx.flush()
        doc = Document(workspace_id=workspace, data={"objects": {}, "order": []})
        tx.add(doc)
        await tx.flush()
        blob = _subject()
        asset = Asset(workspace_id=workspace, kind="image", ext="png", storage_key="subject.png",
            bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest(), width=32, height=32, details={})
        tx.add(asset)
        await tx.flush()
        (root / "subject.png").write_bytes(blob)
        await grant(tx, workspace, uuid.uuid4().hex, 100, "owner")
        await publish(tx, catalog, published_by="composition-test")
        session = CoreSession(workspace_id=workspace, source_canvas_id=doc.id, canvas_id=doc.id,
            user_id="owner", language="zh-CN")
        tx.add(session)
        await tx.flush()
        run = await create_run(tx, RunInput(
            session_id=session.id, workspace_id=workspace, user_id="owner", request_key=uuid.uuid4().hex,
            request_hash="a" * 64, snapshot={}, scope={"tools": ["text_layout", "compose_layers"], "write_canvas": True,
                "readable_assets": [str(asset.id)]}, invariants={},
            version_lock={"tool_catalog": catalog.lock.model_dump()}), CoreSettings(credits_limit=100))
        await transition(tx, "run", run.id, 0, "planning")
        await transition(tx, "run", run.id, 1, "running")
        plan = CorePlan(run_id=run.id, version=1, writer="rule", proposal={}, content_hash="b" * 64,
            rationale={}, link_constraints={}, result_type="generate", stage="final", milestones=[],
            self_check={}, expansion_rules={}, version_lock={})
        tx.add(plan)
        await tx.flush()
        run.current_plan_id = plan.id
        ids = {"run": run.id, "doc": doc.id, "asset": asset.id, "plan": plan.id, "workspace": workspace}
    try:
        yield factory, root, config, catalog, ids
    finally:
        async with factory.begin() as tx:
            await tx.execute(update(Job).where(Job.workspace_id == workspace).values(
                status="completed", phase="completed", next_poll_at=None))
        await engine.dispose()


async def _admit(tx, catalog, config, ids, key, tool, parameters, depends=(), *, suppress_canvas=False):
    task = await create_task(tx, TaskInput(run_id=ids["run"], plan_id=ids["plan"], plan_version=1,
        task_key=key, kind="call_tool", tool_ref=tool, tool_version="1", idempotency_key=key + "-key",
        parameters=parameters, scope={}))
    await transition(tx, "task", task.id, 0, "validated")
    for parent in depends:
        tx.add(CoreTaskDependency(task_id=task.id, depends_on_id=parent, run_id=ids["run"], plan_id=ids["plan"]))
    placement = {"mode": "region", "region": {"x": 0, "y": 0, "width": 280, "height": 280}}
    if suppress_canvas:
        placement["suppress_canvas"] = True
    await record_admission(tx, task, catalog, checks={"legal": True, "types": True, "intent": True},
        review_ref=uuid.uuid4().hex, placement=placement)
    await transition(tx, "task", task.id, 1, "queued")
    job = await enqueue(tx, task.id, config)
    return task.id, job


async def _drive(factory, root, config, job, extra=3):
    """Advance the phase machine to done, then replay the same job three more times."""
    from backend.core.execution.runtime import build_worker
    finished = False
    for _ in range(40):
        built = await build_worker(factory, root, job, client=None, settings=config)
        await built.execute(job)
        async with factory() as tx:
            record = await tx.scalar(select(TaskExecution).where(TaskExecution.job_id == job))
            if record is not None and record.phase == "done":
                finished = True
                break
    if not finished:
        raise AssertionError("composition worker did not finish")
    for _ in range(extra):
        built = await build_worker(factory, root, job, client=None, settings=config)
        await built.execute(job)


def test_text_then_compose_publishes_once(tmp_path):
    async def scenario():
        async with opened(tmp_path) as (factory, root, config, catalog, ids):
            presets = ("feed_square", "story_portrait", "banner_landscape")
            for preset in presets:
                key = "title_" + preset
                async with factory.begin() as tx:
                    text_id, text_job = await _admit(tx, catalog, config, ids, key, "text_layout", {
                        "copy": "月圆人团圆", "font_id": "noto-sans-sc", "size": 48, "color": "#112233",
                        "alignment": "center", "region": "title", "preset": preset}, suppress_canvas=True)
                await _drive(factory, root, config, text_job)
                async with factory() as tx:
                    text = await tx.get(CoreTask, text_id)
                    assert text.status == "succeeded"
                    assert text.output["copy"] == "月圆人团圆"
                    assert text.output["media_type"] == "text"
                    text_version = await tx.get(CoreAssetVersion, uuid.UUID(text.output["core_asset_version_id"]))
                    assert text_version.run_id == ids["run"]
                    assert text_version.task_id == text_id
                    assert text_version.user_id == "owner"
                    assert await tx.scalar(select(func.count()).select_from(CoreAssetVersion).where(
                        CoreAssetVersion.stored_asset_id == uuid.UUID(text.output["asset_id"]))) == 1
                    assert await tx.scalar(select(func.count()).select_from(AssetReference).where(
                        AssetReference.asset_id == uuid.UUID(text.output["asset_id"]))) == 1
                    text_write = await tx.get(CanvasWrite, text_id)
                    assert text_write.history_only is True
                    doc = await tx.get(Document, ids["doc"])
                    assert f"generated-{text_id}" not in doc.data["objects"]
                async with factory.begin() as tx:
                    compose_id, compose_job = await _admit(tx, catalog, config, ids, "poster_" + preset, "compose_layers", {
                        "preset": preset, "fit": "contain",
                        "background": {"kind": "color", "color": "#F4E7C5"},
                        "subject": {"asset_id": str(ids["asset"]), "media_type": "image"},
                        "text_layers": [f"${key}.output"]}, depends=(text_id,))
                await _drive(factory, root, config, compose_job)
                async with factory() as tx:
                    task = await tx.get(CoreTask, compose_id)
                    assert task.status == "succeeded"
                    layers = task.output["layers"]["layers"]
                    assert [layer["role"] for layer in layers] == ["background", "subject", "text"]
                    assert layers[2]["text"] == "月圆人团圆"
                    subject = layers[1]["transform"]
                    canvas = task.output["layers"]["canvas"]
                    assert subject["width"] == subject["height"] > 0
                    assert subject["width"] <= canvas["width"] and subject["height"] <= canvas["height"]
                    if canvas["width"] != canvas["height"]:
                        assert subject["width"] < max(canvas["width"], canvas["height"])
                    writes = await tx.scalar(select(func.count()).select_from(CanvasWrite).where(CanvasWrite.task_id == compose_id))
                    assert writes == 1
                    write = await tx.get(CanvasWrite, compose_id)
                    assert write.history_only is False
                    version = await tx.get(CoreAssetVersion, uuid.UUID(task.output["core_asset_version_id"]))
                    assert version.layers["layers"][1]["transform"]["width"] == subject["width"]
                    links = await tx.scalar(select(func.count()).select_from(CoreAssetLineage).where(
                        CoreAssetLineage.output_version_id == version.id))
                    assert links >= 1
                    ledger = await tx.scalar(select(HostCreditLedger).where(HostCreditLedger.core_task_id == compose_id))
                    assert ledger.charged == 0
            async with factory() as tx:
                doc = await tx.get(Document, ids["doc"])
                assert len(doc.data["objects"]) == 12
    asyncio.run(scenario())


def test_cancel_before_send_does_not_draw(tmp_path):
    async def scenario():
        async with opened(tmp_path) as (factory, root, config, catalog, ids):
            async with factory.begin() as tx:
                text_id, text_job = await _admit(tx, catalog, config, ids, "title", "text_layout", {
                    "copy": "月圆人团圆", "font_id": "noto-sans-sc", "size": 48, "color": "#111111",
                    "alignment": "left", "region": "body", "preset": "story_portrait"})
            from backend.core.execution.runtime import build_worker
            worker = await build_worker(factory, root, text_job, client=None, settings=config)
            assert await worker.cancel(text_job)
            await worker.execute(text_job)
            async with factory() as tx:
                task = await tx.get(CoreTask, text_id)
                assert task.status == "cancelled"
                doc = await tx.get(Document, ids["doc"])
                assert doc.data["objects"] == {}
    asyncio.run(scenario())


def test_leaf_text_draws_and_v1_binding_survives_v2(tmp_path):
    async def scenario():
        async with opened(tmp_path, ToolSettings(text_line_height=1.3)) as (factory, root, config, catalog, ids):
            later = uuid.uuid4().hex
            v2 = build_catalog(Catalog(CatalogConfig(version=later, environment="development", enabled=True,
                capabilities=[])), later, ToolSettings(text_line_height=2.6))
            async with factory.begin() as tx:
                await publish(tx, v2, published_by="composition-v2")
                text_id, text_job = await _admit(tx, catalog, config, ids, "leaf", "text_layout", {
                    "copy": "月圆人团圆", "font_id": "noto-sans-sc", "size": 48, "color": "#111111",
                    "alignment": "left", "region": "title", "preset": "feed_square"})
            await _drive(factory, root, config, text_job)
            async with factory() as tx:
                text = await tx.get(CoreTask, text_id)
                assert text.output["lines"][0]["height"] == round(48 * 1.3)
                doc = await tx.get(Document, ids["doc"])
                assert f"generated-{text_id}" in doc.data["objects"]
                assert doc.data["objects"][f"generated-{text_id}"]["text"] == "月圆人团圆"
                assert (await tx.get(CanvasWrite, text_id)).history_only is False
    asyncio.run(scenario())
