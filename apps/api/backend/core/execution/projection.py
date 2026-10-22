"""Program-owned canvas placeholders. No planner supplies canvas coordinates."""
import copy
from sqlalchemy import select
from backend.core.orm import CoreRun, CoreSession, CoreTask
from backend.infrastructure.orm import Document
from .contracts import WorkerSettings
from .layout import choose
from .orm import ExecutionAdmission
from backend.core.tools.store import load_catalog


async def project_pending(factory, limit):
    async with factory.begin() as tx:
        documents = list(await tx.scalars(select(Document).join(CoreSession, CoreSession.canvas_id == Document.id)
            .join(CoreRun, CoreRun.session_id == CoreSession.id).limit(limit).with_for_update(of=Document, skip_locked=True)))
        for doc in documents:
            tasks = list(await tx.scalars(select(CoreTask).join(CoreRun, CoreRun.id == CoreTask.run_id)
                .join(CoreSession, CoreSession.id == CoreRun.session_id).where(CoreSession.canvas_id == doc.id)))
            data, changed = copy.deepcopy(doc.data), False
            settings = WorkerSettings()
            for task in tasks:
                ident = "generated-" + str(task.id)
                objects = data.setdefault("objects", {})
                existing = objects.get(ident, {})
                if task.status in {"cancelled", "failed", "blocked_by_model", "rejected"}:
                    if existing.get("corePlaceholder"):
                        objects.pop(ident)
                        data["order"] = [key for key in data.get("order", []) if key != ident]
                        changed = True
                    continue
                if task.kind != "call_tool" or task.tool_ref == "detect_subject" or task.status not in {"validated", "queued", "submitted"} or existing:
                    continue
                admission = await tx.scalar(select(ExecutionAdmission).where(ExecutionAdmission.task_id == task.id))
                run = await tx.get(CoreRun, task.run_id)
                if admission is None or not run.scope.get("write_canvas"):
                    continue
                catalog = await load_catalog(tx, run.version_lock["tool_catalog"]["version"])
                tool = catalog.get_tool(task.tool_ref, catalog.lock.version)
                kind = next((p.media_type for p in tool.outputs if p.media_type in {"image", "video"}), None)
                if kind is None:
                    continue
                rect, _, _ = choose(data, admission.placement, settings)
                if rect is not None:
                    objects[ident] = {**rect, "id": ident, "kind": kind, "corePlaceholder": True,
                        "coreTaskId": str(task.id), "busy": True, "label": task.task_key}
                    data.setdefault("order", []).append(ident)
                    changed = True
            if changed:
                doc.data, doc.revision = data, doc.revision + 1
