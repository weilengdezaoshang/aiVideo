"""Asset-card domain on an explicit disposable Postgres. Providers stay fake.

Covers the block acceptance criteria: run-frozen card versions, asset_changed
notification into active runs, delisting fail-closed, worker card tasks, card
parameters in generation resolution, and the ingest recognition pipeline.
"""
import asyncio
import hashlib
import io
import os
import uuid

import pytest
from PIL import Image
from cryptography.fernet import Fernet
from sqlalchemy import select

from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import Binding, Capability, CatalogConfig, TierSpec
from backend.agent.media.credits import grant
from backend.agent.providers import ModelCallError
from backend.core.asset_cards import (create_card, frozen_card_views, resolve_card, set_card_status, update_card)
from backend.core.contracts import CoreSettings, RunInput, TaskInput
from backend.core.execution.contracts import ProviderResult, WorkerSettings
from backend.core.execution.engine import Worker, enqueue, record_admission
from backend.core.execution.local import LocalAdapter
from backend.core.execution.orm import TaskExecution
from backend.core.orm import (CoreAsset, CoreAssetVersion, CoreEvent, CorePlan, CoreRun, CoreSession,
    CoreTask, CoreTaskDependency)
from backend.core.recognition.contracts import RecognitionResult, RecognitionSettings, SubjectRecognition, StyleRecognition
from backend.core.recognition.orm import CoreRecognitionJob
from backend.core.recognition.service import enqueue_recognition
from backend.core.recognition.worker import RecognitionWorker
from backend.core.store import create_run, create_task, transition
from backend.core.tools import build_catalog
from backend.core.tools.store import publish
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Asset, Document, Workspace

DB = os.environ.get("AIVERO_DURABLE_TEST_DB") or os.environ.get("AIVERO_CORE_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated PostgreSQL URL required")


def _png_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (16, 12), "blue").save(stream, format="PNG")
    return stream.getvalue()


def _catalog():
    ident = "cards-pg-" + uuid.uuid4().hex[:10]
    snapshot = {"provider": "mock", "configVersion": ident,
                "cloudModel": "mock-image", "videoModel": "mock-video"}
    capability = Capability(id="text_to_image", enabled=True, outputType="image", maxRefImages=0,
        tiers={"preview": TierSpec(credits=10), "final": TierSpec(credits=20)},
        binding=Binding(version=ident, providerSnapshot=snapshot))
    return build_catalog(Catalog(CatalogConfig(version=ident, environment="development",
        enabled=True, capabilities=[capability])), ident)


async def _seed_run(factory, tmp_path, *, card=None, tools=()):
    """Workspace + document + stored image + running Run that froze `card`."""
    catalog = _catalog()
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    data = _png_bytes()
    (media / "ok.png").write_bytes(data)
    workspace = uuid.uuid4()
    async with factory.begin() as tx:
        tx.add(Workspace(id=workspace))
        await tx.flush()
        doc = Document(workspace_id=workspace, name="画布", data={"objects": {}, "order": []}, revision=1)
        tx.add(doc)
        await tx.flush()
        stored = Asset(workspace_id=workspace, kind="image", ext="png", width=16, height=12,
            bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), storage_key="ok.png")
        tx.add(stored)
        await tx.flush()
        await grant(tx, workspace, uuid.uuid4().hex, 1000, "test")
        await publish(tx, catalog, published_by="test")
        card_row, card_version = (None, None)
        asset_cards = {}
        if card is not None:
            card_row, card_version = await create_card(tx, workspace_id=workspace, user_id="u1",
                card_kind=card.get("card_kind", "style"), name=card.get("name", "水彩插画"),
                description=card.get("description", ""), source_stored_asset=stored,
                starred=card.get("starred", False), notify=False)
            asset_cards[str(card_row.id)] = str(card_version.id)
        session = CoreSession(workspace_id=workspace, source_canvas_id=doc.id, canvas_id=doc.id,
            user_id="u1", language="zh-CN")
        tx.add(session)
        await tx.flush()
        run = await create_run(tx, RunInput(session_id=session.id, workspace_id=workspace, user_id="u1",
            request_key=uuid.uuid4().hex, request_hash="a" * 64,
            snapshot={"request": {"message": "用这个风格生成"}},
            scope={"tools": [*tools], "readable_assets": [str(stored.id)],
                   "readable_cards": list(asset_cards), "write_canvas": False},
            invariants={}, version_lock={"tool_catalog": catalog.lock.model_dump(),
                "supervisor": "rule-v1", "asset_cards": asset_cards}), CoreSettings(credits_limit=1000))
        await transition(tx, "run", run.id, 0, "planning")
        await transition(tx, "run", run.id, 1, "running")
        plan = CorePlan(run_id=run.id, version=1, writer="rule", proposal={}, content_hash="b" * 64,
            rationale={}, link_constraints={}, result_type="generate", stage="final",
            milestones=[], self_check={}, expansion_rules={}, version_lock={})
        tx.add(plan)
        await tx.flush()
        run.current_plan_id = plan.id
        ids = {"workspace": workspace, "doc": doc.id, "asset": stored.id, "run": run.id,
            "plan": plan.id, "session": session.id, "card": card_row.id if card_row else None,
            "card_version": card_version.id if card_version else None}
        return catalog, media, ids


async def _queue_task(factory, ids, catalog, name, parameters, *, depends_on=None):
    async with factory.begin() as tx:
        tool = catalog.get_tool(name, catalog.lock.version)
        key = name + uuid.uuid4().hex[:8]
        task = await create_task(tx, TaskInput(run_id=ids["run"], plan_id=ids["plan"], plan_version=1,
            task_key=key, kind="call_tool", tool_ref=name,
            tool_version=tool.version, idempotency_key=uuid.uuid4().hex, parameters=parameters, scope={}))
        await transition(tx, "task", task.id, 0, "validated")
        await record_admission(tx, task, catalog, checks={"legal": True, "types": True, "intent": True},
            review_ref="review", placement={})
        await transition(tx, "task", task.id, 1, "queued")
        job_id = await enqueue(tx, task.id)
        if depends_on is not None:
            tx.add(CoreTaskDependency(task_id=task.id, depends_on_id=depends_on,
                run_id=ids["run"], plan_id=ids["plan"]))
        return task.id, job_id, key


class CaptureProvider:
    """Fake generation interface: records resolved parameters, returns one image."""
    idempotent = True
    query_registration = True
    cancellable = False

    def __init__(self, root):
        self.root = root
        self.submitted = []

    async def submit(self, registration_id, params, idempotency_key):
        self.submitted.append(params)
        return ProviderResult("succeeded", registration_id, data=_png_bytes(), ext="png", actual_credits=1)

    async def query(self, task_id, registration_id):
        return None

    async def download(self, locator, kind):
        raise ValueError("no deferred artifacts")

    async def cancel(self, task_id):
        raise ValueError("not cancellable")


def test_run_freezes_card_and_update_notifies_active_run(tmp_path):
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        try:
            catalog, media, ids = await _seed_run(factory, tmp_path,
                card={"card_kind": "style", "name": "水彩插画", "description": "淡雅水彩"})
            card_id = ids["card"]
            async with factory.begin() as tx:
                run = await tx.get(CoreRun, ids["run"])
                frozen = await resolve_card(tx, run, str(card_id))
                assert frozen["name"] == "水彩插画"
            # Mid-run edit appends a version and emits asset_changed into this run.
            async with factory.begin() as tx:
                await update_card(tx, card_id, ids["workspace"], 1, name="厚涂插画")
            async with factory() as tx:
                run = await tx.get(CoreRun, ids["run"])
                events = list(await tx.scalars(select(CoreEvent.event_type).where(
                    CoreEvent.run_id == ids["run"], CoreEvent.event_kind == "domain")))
                assert "asset_changed" in events
                # The run keeps using its frozen version.
                frozen = await resolve_card(tx, run, str(card_id))
                assert frozen["name"] == "水彩插画"
                assert frozen["version_number"] == 1
                views = await frozen_card_views(tx, run, limit=10)
                assert [view["name"] for view in views] == ["水彩插画"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_delisted_card_fails_closed_everywhere(tmp_path):
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        try:
            catalog, media, ids = await _seed_run(factory, tmp_path,
                card={"card_kind": "style", "name": "水彩插画"})
            card_id = ids["card"]
            async with factory.begin() as tx:
                await set_card_status(tx, card_id, ids["workspace"], 1, status="removed")
            async with factory() as tx:
                run = await tx.get(CoreRun, ids["run"])
                with pytest.raises(ValueError, match="card_unavailable"):
                    await resolve_card(tx, run, str(card_id))
                # A delisted card cannot be edited back to life.
                from backend.core.asset_cards import CardRejected, update_card
                with pytest.raises(CardRejected, match="card_not_editable"):
                    await update_card(tx, card_id, ids["workspace"], 2, name="复活")
                # Context drops delisted cards entirely.
                views = await frozen_card_views(tx, run, limit=10)
                assert views == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_worker_executes_card_task_and_card_parameter_generation(tmp_path):
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        try:
            catalog, media, ids = await _seed_run(factory, tmp_path,
                card={"card_kind": "style", "name": "水彩插画", "description": "淡雅水彩"},
                tools=["create_asset_card", "update_asset_card", "text_to_image_preview"])
            config = WorkerSettings(ticket_key=Fernet.generate_key().decode(), retry_jitter=0)
            tool = catalog.get_tool("create_asset_card", catalog.lock.version)
            worker = Worker(factory, media, LocalAdapter(tool, factory, media), settings=config)
            task_id, job_id, _key = await _queue_task(factory, ids, catalog, "create_asset_card",
                {"card_kind": "subject", "name": "橘子", "description": "一只橘猫"})
            await worker.execute(job_id)
            async with factory() as tx:
                task = await tx.get(CoreTask, task_id)
                assert task.status == "succeeded", task.error
                assert task.output["media_type"] == "card"
                created = await tx.get(CoreAsset, uuid.UUID(task.output["card_id"]))
                assert created is not None
                version = await tx.get(CoreAssetVersion, created.current_version_id)
                assert version.category == "card" and version.card_kind == "subject"
                # The run's own card task must not echo an asset_changed back to itself.
                echoed = list(await tx.scalars(select(CoreEvent.event_type).where(
                    CoreEvent.run_id == ids["run"], CoreEvent.event_kind == "domain",
                    CoreEvent.event_type == "asset_changed")))
                assert echoed == []

            # Generation with the run-frozen style card merges its description into the prompt.
            card_id, card_version_id = ids["card"], ids["card_version"]
            capture = CaptureProvider(media)
            gen_worker = Worker(factory, media, capture, settings=config)
            card_ref = {"card_id": str(card_id), "media_type": "card"}
            gen_tool = catalog.get_tool("text_to_image_preview", catalog.lock.version)
            gen_task_id, gen_job_id, _gen_key = await _queue_task(factory, ids, catalog, "text_to_image_preview",
                {"prompt": "画一张海报", "count": 1, "width": gen_tool.limits["width"],
                 "height": gen_tool.limits["height"], "style_cards": [card_ref]})
            await gen_worker.execute(gen_job_id)
            async with factory() as tx:
                task = await tx.get(CoreTask, gen_task_id)
                assert task.status == "succeeded", task.error
                from backend.core.execution.orm import TaskExecution
                execution = await tx.get(TaskExecution, gen_task_id)
                assert "水彩插画" in execution.resolved_parameters["prompt"]
                assert execution.resolved_parameters["_card_refs"] == [
                    {"card_id": str(card_id), "usage": "style", "version_id": str(card_version_id)}]
                assert "style_cards" not in execution.resolved_parameters
                assert any("水彩插画" in params["prompt"] for params in capture.submitted)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_recognition_pipeline_applies_retries_and_skips(tmp_path):
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        try:
            catalog, media, ids = await _seed_run(factory, tmp_path)
            settings = RecognitionSettings(max_attempts=2, retry_backoff_seconds=0.01,
                lease_seconds=5, scan_limit=5)

            class StubAnalyzer:
                available = True

                def __init__(self):
                    self.calls = 0

                def label(self):
                    return "stub"

                async def recognize(self, data, *, ext="png"):
                    self.calls += 1
                    return RecognitionResult(subject=SubjectRecognition(category="猫", description="橘猫"),
                        style=StyleRecognition(tags=["水彩"], description="淡雅水彩"),
                        overall="水彩橘猫", model="stub")

                async def answer(self, data, question, *, ext="png", language="zh"):
                    raise AssertionError("answer not used")

            async with factory.begin() as tx:
                stored = await tx.get(Asset, ids["asset"])
                await enqueue_recognition(tx, workspace_id=ids["workspace"], stored_asset=stored,
                    requested_by="u1", source="upload", settings=settings)
                from backend.core.recognition.service import job_request_key
                key = job_request_key(stored)
            worker = RecognitionWorker(factory, media, StubAnalyzer(), settings=settings)
            assert await worker.run_once() >= 1
            async with factory() as tx:
                job = await tx.scalar(select(CoreRecognitionJob).where(
                    CoreRecognitionJob.request_key == key))
                assert job.status == "succeeded"
                version = await tx.get(CoreAssetVersion, job.core_asset_version_id)
                assert version.recognition["subject"]["category"] == "猫"
                assert version.recognition["content_hash"] == stored.sha256
                assert version.recognition["source"] == "vision"
                # Idempotent re-run must not append a second recognition version.
                head_before = version.version
            assert await worker.run_once() == 0
            async with factory() as tx:
                card_asset = await tx.get(CoreAsset, job.core_asset_id)
                head = await tx.get(CoreAssetVersion, card_asset.current_version_id)
                assert head.version == head_before

            # Failure path: attempts increment, then the job fails permanently.
            class FailingAnalyzer:
                available = True

                def label(self):
                    return "failing"

                async def recognize(self, data, *, ext="png"):
                    raise ModelCallError("MODEL_HTTP_ERROR", "boom")

                async def answer(self, data, question, *, ext="png", language="zh"):
                    raise ModelCallError("MODEL_HTTP_ERROR", "boom")

            async with factory.begin() as tx:
                original = await tx.get(Asset, ids["asset"])
                stored = Asset(workspace_id=original.workspace_id, kind="image", ext="png",
                    width=original.width, height=original.height, bytes=original.bytes,
                    sha256=original.sha256, storage_key=original.storage_key)
                tx.add(stored)
                await tx.flush()
                await enqueue_recognition(tx, workspace_id=ids["workspace"], stored_asset=stored,
                    requested_by="u1", source="upload", settings=settings)
                fail_key = job_request_key(stored)
            failing = RecognitionWorker(factory, media, FailingAnalyzer(), settings=settings)
            await failing.run_once()
            async with factory() as tx:
                job = await tx.scalar(select(CoreRecognitionJob).where(
                    CoreRecognitionJob.request_key == fail_key))
                assert job.status == "pending" and job.attempt == 1
            await asyncio.sleep(0.05)
            await failing.run_once()
            async with factory() as tx:
                job = await tx.scalar(select(CoreRecognitionJob).where(
                    CoreRecognitionJob.request_key == fail_key))
                assert job.status == "failed" and job.attempt >= 2

            # Unavailable analyzer keeps jobs pending without consuming attempts.
            from backend.core.recognition.analyzer import NullAnalyzer
            async with factory.begin() as tx:
                original = await tx.get(Asset, ids["asset"])
                stored = Asset(workspace_id=original.workspace_id, kind="image", ext="png",
                    width=original.width, height=original.height, bytes=original.bytes,
                    sha256=original.sha256, storage_key=original.storage_key)
                tx.add(stored)
                await tx.flush()
                await enqueue_recognition(tx, workspace_id=ids["workspace"], stored_asset=stored,
                    requested_by="u1", source="upload", settings=settings)
                null_key = job_request_key(stored)
            null_worker = RecognitionWorker(factory, media, NullAnalyzer(), settings=settings)
            await null_worker.run_once()
            async with factory() as tx:
                job = await tx.scalar(select(CoreRecognitionJob).where(
                    CoreRecognitionJob.request_key == null_key))
                assert job.status == "pending" and job.attempt == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_created_card_chains_into_generation(tmp_path):
    """$tN.output card references work for cards the Run created itself."""
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        try:
            catalog, media, ids = await _seed_run(factory, tmp_path,
                tools=["create_asset_card", "text_to_image_preview"])
            config = WorkerSettings(ticket_key=Fernet.generate_key().decode(), retry_jitter=0)
            create_tool = catalog.get_tool("create_asset_card", catalog.lock.version)
            card_worker = Worker(factory, media, LocalAdapter(create_tool, factory, media), settings=config)
            first_id, first_job, first_key = await _queue_task(factory, ids, catalog, "create_asset_card",
                {"card_kind": "style", "name": "厚涂风景", "description": "厚重笔触的风景质感"})
            await card_worker.execute(first_job)
            async with factory() as tx:
                first = await tx.get(CoreTask, first_id)
                assert first.status == "succeeded", first.error
            capture = CaptureProvider(media)
            gen_worker = Worker(factory, media, capture, settings=config)
            gen_tool = catalog.get_tool("text_to_image_preview", catalog.lock.version)
            chained_id, chained_job, _chained_key = await _queue_task(factory, ids, catalog,
                "text_to_image_preview",
                {"prompt": "画一片山林", "count": 1, "width": gen_tool.limits["width"],
                 "height": gen_tool.limits["height"], "style_cards": [f"${first_key}.output"]},
                depends_on=first_id)
            await gen_worker.execute(chained_job)
            async with factory() as tx:
                task = await tx.get(CoreTask, chained_id)
                assert task.status == "succeeded", task.error
                execution = await tx.get(TaskExecution, chained_id)
                assert "厚涂风景" in execution.resolved_parameters["prompt"]
                refs = execution.resolved_parameters["_card_refs"]
                assert refs[0]["card_id"] == first.output["card_id"]
                assert refs[0]["version_id"] == first.output["version_id"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())
