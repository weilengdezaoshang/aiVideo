"""Owned real PG rows, production supervisor/worker, and explicitly fake generation."""

import asyncio
import hashlib
import io
import uuid
from pathlib import Path
from PIL import Image
from cryptography.fernet import Fernet
from sqlalchemy import select
from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.agent.media.credits import grant
from backend.core.events.processor import Processor
from backend.core.events.registry import Registry
from backend.core.events.store import accept
from backend.core.events.orm import EventReceipt
from backend.core.execution.contracts import ProviderResult
from backend.core.execution.engine import Worker, scan_ready
from backend.core.execution.fake import FakeProvider
from backend.core.execution.local import LocalAdapter
from backend.core.execution.masks import save_mask
from backend.core.execution.projection import project_pending
from backend.core.orm import CoreRun, CoreTask, CoreEvent
from backend.core.supervisor.engine import Supervisor, register, stop
from backend.core.supervisor.planners import FakePlanner
from backend.core.supervisor.service import start, answer
from backend.core.tools import build_catalog, ToolCatalog, CatalogSnapshot
from backend.core.tools.contracts import ToolDefinition, Execution, Port, Price
from backend.core.tools.definitions import reference_schema
from backend.core.tools.store import publish
from backend.infrastructure.database import (
    DatabaseSettings,
    create_async_database_engine,
    create_async_session_factory,
)
from backend.infrastructure.orm import Workspace, Document, Asset


def png(mask=False, right=False):
    image = Image.new("L" if mask else "RGBA", (8, 8), 0 if mask else "white")
    if mask:
        for x in range(4 if right else 0, 8 if right else 4):
            for y in range(8):
                image.putpixel((x, y), 255)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class SubjectFake(LocalAdapter):
    def __init__(self, tool, factory, root, labels):
        super().__init__(tool, factory, root)
        self.labels = labels

    async def compute(self, ident, params):
        if self.tool.name != "detect_subject":
            return await super().compute(ident, params)
        from backend.core.tools.detection import ImageIdentity, RawCandidate, normalize_candidates

        image = params["image"]
        source = ImageIdentity(
            asset_id=image["asset_id"], sha256=image["sha256"], width=8, height=8
        )
        raw = []
        for i, label in enumerate(self.labels):
            data, handle = png(True, bool(i)), ident + ":" + str(i)
            save_mask(self.root, handle, data)
            raw.append(RawCandidate(data, handle, label, 0.99))
        result = normalize_candidates(raw, source, ident)
        return ProviderResult("succeeded", ident, output=result.model_dump(mode="json"))


class Harness:
    def __init__(self, urls, root, case, suite):
        self.urls, self.root, self.case, self.suite = urls, Path(root), case, suite
        self.root.mkdir(parents=True)
        self.engine = create_async_database_engine(
            DatabaseSettings(url=urls["AIVERO_RELIABILITY_DB"], pool_size=12, max_overflow=4)
        )
        self.factory = create_async_session_factory(self.engine)
        self.worker_settings = suite.worker.model_copy(
            update={"ticket_key": Fernet.generate_key().decode()}
        )
        self.event_settings = suite.events
        registry = Registry()
        register(registry)
        self.processor = Processor(self.factory, self.event_settings, registry)
        self.run_ids = []
        self.fault_used = False

    async def setup(self):
        base = build_catalog(
            Catalog(
                CatalogConfig(
                    version="reliability", environment="development", enabled=True, capabilities=[]
                )
            ),
            "reliability",
        )
        tools = list(base.snapshot.tools)
        for name, kind in (("edit", "image"), ("video", "video")):
            tools.append(
                ToolDefinition(
                    name=name,
                    version="1",
                    description="Explicit reliability fake only",
                    parameters={
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "image": reference_schema("image"),
                        },
                        "required": ["prompt", "image"],
                        "additionalProperties": False,
                    },
                    result_schema={"type": "object"},
                    inputs=[Port(path="image", media_type="image")],
                    outputs=[Port(path="output", media_type=kind)],
                    mode="generate",
                    price=Price(kind="fixed", credits=self.suite.quoted_credits),
                    limits={},
                    failures=[],
                    execution=Execution(
                        adapter="execution_test_fake",
                        provider_idempotent=self.case.provider.idempotent,
                        cancellable=self.case.provider.cancellable,
                        cancellation="provider" if self.case.provider.cancellable else "none",
                        result_url_policy="authenticated_asset",
                        sla_seconds=self.suite.service_sla_seconds,
                    ),
                )
            )
        self.catalog = ToolCatalog(CatalogSnapshot(version=uuid.uuid4().hex, tools=tools))
        async with self.factory.begin() as tx:
            workspace = Workspace()
            tx.add(workspace)
            await tx.flush()
            self.workspace = workspace.id
            await grant(tx, workspace.id, "initial", self.suite.initial_credits, "reliability")
            (self.root / "source.png").write_bytes(png())
            source = Asset(
                workspace_id=workspace.id,
                kind="image",
                ext="png",
                storage_key="source.png",
                bytes=len(png()),
                sha256=hashlib.sha256(png()).hexdigest(),
                width=8,
                height=8,
                details={},
            )
            tx.add(source)
            await tx.flush()
            self.source = source.id
            await publish(tx, self.catalog, published_by="reliability")
        await self.new_run()
        self.script = self.render_script(
            self.case.planner
            or [
                {
                    "plan": {
                        "goal": self.case.message,
                        "steps": [
                            {
                                "id": "t1",
                                "tool": "edit",
                                "parameters": {
                                    "prompt": "edit",
                                    "image": {"asset_id": "$asset", "media_type": "image"},
                                },
                            },
                            {
                                "id": "t2",
                                "tool": "video",
                                "parameters": {"prompt": "video", "image": "$t1.output"},
                                "depends_on": ["t1"],
                            },
                        ],
                    },
                    "confidence": 1,
                    "tokens": 2,
                }
            ]
        )
        if self.case.probe == "mask":
            self.supervisor = Supervisor(self.factory)
        else:
            self.supervisor = Supervisor(self.factory, FakePlanner(self.script))
        self.fake = FakeProvider(self.root / "provider.sqlite", self.case.provider)
        self.second = (
            FakeProvider(self.root / "second.sqlite", self.case.second_provider)
            if self.case.second_provider
            else self.fake
        )

    def render_script(self, script):
        def render(value):
            if isinstance(value, str):
                return str(self.source) if value == "$asset" else value
            if isinstance(value, list):
                return [render(v) for v in value]
            if isinstance(value, dict):
                return {k: render(v) for k, v in value.items()}
            return value

        output = render(script)
        for entry in output:
            entry.setdefault("invariants", self.invariants)
            entry.setdefault("raw_output", {"script": entry["plan"]})
        return output

    async def new_run(self):
        async with self.factory.begin() as tx:
            doc = Document(
                workspace_id=self.workspace,
                data={
                    "objects": {
                        "source": {
                            "id": "source",
                            "kind": "image",
                            "assetId": str(self.source),
                            "width": 8,
                            "height": 8,
                            "x": 0,
                            "y": 0,
                        }
                    },
                    "order": ["source"],
                },
            )
            tx.add(doc)
            await tx.flush()
            row = await start(
                tx,
                workspace_id=self.workspace,
                user_id="reliability",
                document_id=doc.id,
                request_key=uuid.uuid4().hex,
                request={
                    "message": self.case.message,
                    "selected_assets": [str(self.source)],
                    "anchor_id": "source",
                },
                budget=self.suite.run_budget,
                token_budget=self.suite.token_budget,
                max_rounds=self.suite.max_thought_rounds,
                mode=self.case.mode,
            )
            self.invariants = row.invariants
            self.run_ids.append(row.id)
        return row.id

    async def events(self, reverse=False):
        async with self.factory.begin() as tx:
            events = list(
                await tx.scalars(
                    select(CoreEvent)
                    .outerjoin(EventReceipt, EventReceipt.event_id == CoreEvent.id)
                    .where(CoreEvent.run_id.in_(self.run_ids), EventReceipt.event_id.is_(None))
                    .order_by(CoreEvent.run_sequence)
                )
            )
            for event in reversed(events) if reverse else events:
                await accept(tx, {"event_id": str(event.id), "run_id": str(event.run_id)})
        for _ in range(self.suite.max_steps * len(self.run_ids)):
            if not any(await self.processor.tick()):
                break

    async def tasks(self):
        async with self.factory() as tx:
            return list(
                await tx.scalars(
                    select(CoreTask)
                    .where(CoreTask.run_id.in_(self.run_ids))
                    .order_by(CoreTask.task_key)
                )
            )

    def provider(self, task):
        if task.tool_ref in {"cutout", "detect_subject"}:
            return SubjectFake(
                self.catalog.get_tool(task.tool_ref, self.catalog.lock.version),
                self.factory,
                self.root,
                self.case.subjects,
            )
        return self.second if task.task_key == "t2" else self.fake

    async def drive(self):
        from .process import kill_checkpoint

        for _ in range(self.suite.max_steps * len(self.run_ids)):
            if self.case.fault.checkpoint == "wake" and not self.fault_used:
                # Persist inbox first; crash the consumer after its receipt + wakeup transaction.
                async with self.factory.begin() as tx:
                    for event in await tx.scalars(
                        select(CoreEvent).where(CoreEvent.run_id.in_(self.run_ids))
                    ):
                        await accept(tx, {"event_id": str(event.id), "run_id": str(event.run_id)})
                await kill_checkpoint(self, "wake")
                self.fault_used = True
            await self.events(reverse=self.case.probe == "event_order")
            checkpoint = self.case.fault.checkpoint
            if not self.fault_used and checkpoint in {"planner", "plan"}:
                if checkpoint == "plan":
                    await self.supervisor.tick()
                await kill_checkpoint(self, checkpoint)
                self.fault_used = True
            else:
                await self.supervisor.tick()
            await scan_ready(self.factory)
            await project_pending(self.factory, self.suite.max_steps)
            tasks = await self.tasks()
            for task in tasks:
                if task.status not in {"queued", "submitted"}:
                    continue
                if (
                    not self.fault_used
                    and checkpoint in {"t1", "t2", "provider_accepted"}
                    and task.task_key == ("t2" if checkpoint == "t2" else "t1")
                ):
                    await kill_checkpoint(self, checkpoint, task.job_id)
                    self.fault_used = True
                else:
                    worker = Worker(
                        self.factory, self.root, self.provider(task), settings=self.worker_settings
                    )
                    await worker.execute(task.job_id)
                    if self.case.probe in {"cancel", "inflight_stop"} and not self.fault_used:
                        async with self.factory.begin() as tx:
                            row = await tx.get(CoreRun, task.run_id, with_for_update=True)
                            await stop(tx, row, "cancelled", "reliability cancellation", "cancel")
                            # Before settlement the Run must not claim a terminal state.
                            submitted = await tx.scalar(
                                select(CoreTask.id).where(
                                    CoreTask.run_id == row.id, CoreTask.status == "submitted"
                                )
                            )
                            if submitted:
                                assert row.status == "paused" and row.pause_reason == "waiting_info"
                        self.fault_used = True
            if self.case.probe == "callbacks":
                from backend.core.execution.orm import TaskExecution

                async with self.factory() as tx:
                    for task in tasks:
                        execution = await tx.get(TaskExecution, task.id)
                        if execution and execution.attempt_id:
                            from backend.core.orm import CoreTaskAttempt

                            attempt = await tx.get(CoreTaskAttempt, execution.attempt_id)
                            for _, result in await self.provider(task).callbacks():
                                if result.task_id == attempt.provider_task_id:
                                    assert await Worker(
                                        self.factory,
                                        self.root,
                                        self.provider(task),
                                        settings=self.worker_settings,
                                    ).callback(task.job_id, attempt.provider_ref, result)
            for task in await self.tasks():
                if task.status == "waiting_user" and self.case.choice is not None:
                    async with self.factory.begin() as tx:
                        await answer(
                            tx,
                            task_id=task.id,
                            option_id=self.case.choice,
                            request_key="choice",
                            workspace_id=self.workspace,
                            user_id="reliability",
                        )
            async with self.factory() as tx:
                statuses = list(
                    await tx.scalars(select(CoreRun.status).where(CoreRun.id.in_(self.run_ids)))
                )
            if all(s in {"completed", "failed", "cancelled"} for s in statuses):
                return
            await asyncio.sleep(self.suite.poll_seconds)
        raise AssertionError("scenario did not reach a terminal state")
