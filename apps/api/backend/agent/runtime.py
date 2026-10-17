"""Agent service: admission, queries and event streaming on top of the durable Kernel.

Execution mechanics (leases, epochs, recovery, stop) live in kernel.py. The module
constants below stay the deployment-tunable limits the Kernel reads at call time.
"""
from __future__ import annotations

import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from backend.agent.kernel import ACTIVE, Kernel, Limits
from backend.agent import memory
from backend.agent.persona import DEFAULT_PERSONA
from backend.agent.orm import AgentEvent, AgentRun, AgentSession
from backend.agent.util import encoded, error, now, public_error, stamp  # noqa: F401
from backend.infrastructure.orm import Asset, AssetReference, Document

MAX_MODEL_CALLS = 20
MAX_TOOL_CALLS = 24
TOOL_TIMEOUT_S = 15
TOOL_HEARTBEAT_S = 5


def _body(raw):
    request = raw.get("clientRequestId")
    message = raw.get("message")
    model = raw.get("modelId")
    revision = raw.get("baseRevision")
    selected = raw.get("selectedObjectIds", [])
    if not isinstance(request, str) or not 1 <= len(request) <= 128:
        raise HTTPException(400, "clientRequestId 无效")
    if not isinstance(message, str) or not message.strip() or len(message) > 8000:
        raise HTTPException(400, "消息不能为空且不超过 8000 字")
    if not isinstance(model, str) or not 1 <= len(model) <= 128:
        raise HTTPException(400, "modelId 无效")
    if type(revision) is not int or revision < 1:
        raise HTTPException(428, "必须提供 baseRevision")
    if (not isinstance(selected, list) or len(selected) > 200
            or any(not isinstance(x, str) or not 1 <= len(x) <= 200 for x in selected)):
        raise HTTPException(400, "selectedObjectIds 无效")
    return {"clientRequestId": request, "message": message.strip(), "modelId": model,
            "baseRevision": revision, "selectedObjectIds": list(dict.fromkeys(selected))}


class AgentService(Kernel):
    def limits(self) -> Limits:
        return Limits(MAX_MODEL_CALLS, MAX_TOOL_CALLS, TOOL_TIMEOUT_S, TOOL_HEARTBEAT_S)

    async def advance(self, run_id, owner, model_client, tools=None, media_root=None):
        async with self.factory() as db:
            run = await db.get(AgentRun, run_id)
            media = run is not None and run.phase == "media"
        if media:
            import os
            from pathlib import Path
            from backend.agent.media.catalog import Catalog
            from backend.agent.media.service import MediaHost
            path = os.environ.get("AIVERO_PRODUCTION_HOST_CATALOG_FILE")
            host = MediaHost(self.factory, Catalog.load(Path(path) if path else None), media_root)
            from backend.agent.media.reviewer import review_one
            if await review_one(host, run_id, model_client):
                return {"reviewed": True}
            return await host.tick(run_id)
        return await super().advance(run_id, owner, model_client, tools, media_root)

    async def recover_due(self, limit=100):
        from backend.agent.media.service import MediaHost
        media = await MediaHost(self.factory).recover_due(limit)
        return media + await super().recover_due(limit)

    async def _session(self, db, workspace, document, *, lock=False):
        query = select(AgentSession).where(AgentSession.workspace_id == workspace,
            AgentSession.source_document_id == document, AgentSession.principal == "")
        if lock:
            query = query.with_for_update()
        return await db.scalar(query)

    async def create_run(self, workspace_id, document_id, body, route, principal="", persona=None):
        from backend.agent.config import AgentRoute
        from backend.agent.tools import ToolError, project_snapshot
        body = _body(body)
        fingerprint = hashlib.sha256(encoded(body).encode()).hexdigest()
        async with self.factory.begin() as db:
            # Also serializes first-session creation and concurrent document deletion.
            doc = await db.get(Document, document_id, with_for_update=True)
            if doc is None or doc.workspace_id != workspace_id:
                raise HTTPException(404, "文档不存在")
            session = await self._session(db, workspace_id, document_id, lock=True)
            if session:
                previous = await db.scalar(select(AgentRun).where(AgentRun.session_id == session.id,
                    AgentRun.client_request_id == body["clientRequestId"]))
                if previous:
                    if previous.request_hash != fingerprint:
                        raise HTTPException(409, "同一请求标识不能更改消息")
                    return {"run": await self._project(db, previous), "sessionId": str(session.id), "replayed": True}
                if session.closed_at:
                    raise HTTPException(409, "画布会话已关闭")
                active = await db.scalar(select(AgentRun.id).where(AgentRun.session_id == session.id,
                    AgentRun.status.in_(ACTIVE)))
                if active:
                    raise HTTPException(409, "当前画布已有运行中的 Agent")
            if doc.revision != body["baseRevision"]:
                raise HTTPException(409, "画布版本已变化，请保存并重新发送")
            if route is None:
                raise HTTPException(503, "所选 Agent 模型当前不可用")
            route_snapshot = route.snapshot() if hasattr(route, "snapshot") else dict(route)
            route_snapshot = AgentRoute.from_snapshot(route_snapshot).snapshot()
            if route_snapshot.get("id") != body["modelId"]:
                raise HTTPException(400, "模型路由不匹配")
            try:
                snapshot = project_snapshot({**doc.data, "id": str(doc.id), "name": doc.name,
                    "revision": doc.revision}, body["selectedObjectIds"])
            except ToolError as exc:
                raise HTTPException(409 if exc.code == "SELECTION_CHANGED" else 400, str(exc)) from exc
            if session is None:
                session = AgentSession(workspace_id=workspace_id, document_id=document_id,
                    source_document_id=document_id, cursor=0)
                db.add(session)
                await db.flush()
            references = set()

            def collect(value):
                if isinstance(value, dict):
                    if isinstance(value.get("assetId"), str) and value["assetId"]:
                        references.add(uuid.UUID(value["assetId"]))
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
            try:
                collect(snapshot)
            except ValueError:
                raise HTTPException(400, "画布资产标识无效") from None
            assets = {}
            if references:
                rows = list(await db.scalars(select(Asset).where(Asset.id.in_(references))
                    .order_by(Asset.id).with_for_update()))
                if len(rows) != len(references) or any(a.workspace_id != workspace_id for a in rows):
                    raise HTTPException(404, "画布引用资产不可用")
                assets = {str(a.id): {"storage_key": a.storage_key, "sha256": a.sha256,
                    "bytes": a.bytes, "ext": a.ext, "kind": a.kind} for a in rows}
            previous_runs = list(await db.scalars(select(AgentRun).where(AgentRun.session_id == session.id,
                or_(and_(AgentRun.status == "completed", AgentRun.reply.is_not(None)),
                    and_(AgentRun.status == "waiting_input", AgentRun.waiting.is_not(None))))
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(10)))
            history, sources = memory.episodic(list(reversed(previous_runs)), doc.revision)
            run = AgentRun(session_id=session.id, workspace_id=workspace_id,
                client_request_id=body["clientRequestId"], request_hash=fingerprint, principal=principal,
                message=body["message"], route=route_snapshot, snapshot=snapshot, assets=assets,
                history=history, persona=(persona or DEFAULT_PERSONA).model_dump(mode="json"),
                counters={"memorySources": sources}, stage="plan", status="queued", phase="model", state_version=0, execution_epoch=0,
                model_call_count=0, tool_call_count=0)
            db.add(run)
            await db.flush()
            for ident in references:
                db.add(AssetReference(asset_id=ident, owner_type="agent_run", owner_id=str(run.id)))
            self._wake(db, run)
            projected = await self._event(db, session, run, "run.created")
            return {"run": projected, "sessionId": str(session.id), "replayed": False}

    async def get_run(self, workspace_id, run_id, principal=None):
        async with self.factory.begin() as db:
            _, run = await self._lock_run(db, run_id, workspace_id)
            if run.phase == "media" and principal is not None and run.principal != principal:
                raise HTTPException(404, "创作运行不存在")
            return await self._project(db, run)

    async def snapshot(self, workspace_id, document_id, client_request_id=None, after_seq=0):
        async with self.factory.begin() as db:
            doc = await db.get(Document, document_id)
            if doc is None or doc.workspace_id != workspace_id:
                raise HTTPException(404, "文档不存在")
            session = await self._session(db, workspace_id, document_id, lock=True)
            empty = {"sessionId": None, "documentId": str(document_id), "cursor": 0,
                "runs": [], "requestedRun": None}
            if session is None:
                return empty
            runs = list(await db.scalars(select(AgentRun).where(AgentRun.session_id == session.id)
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(50)))
            projected = [await self._project(db, r) for r in reversed(runs)]
            requested = None
            if client_request_id:
                row = await db.scalar(select(AgentRun).where(AgentRun.session_id == session.id,
                    AgentRun.client_request_id == client_request_id))
                requested = await self._project(db, row) if row else None
            return {"sessionId": str(session.id), "documentId": str(document_id),
                "cursor": session.cursor, "runs": projected, "requestedRun": requested}

    async def events(self, workspace_id, document_id, after_seq=0):
        if type(after_seq) is not int or after_seq < 0:
            raise HTTPException(400, "事件游标无效")
        async with self.factory.begin() as db:
            doc = await db.get(Document, document_id)
            if doc is None or doc.workspace_id != workspace_id:
                raise HTTPException(404, "文档不存在")
            session = await self._session(db, workspace_id, document_id, lock=True)
            if session is None:
                return {"events": [], "cursor": 0}
            values = list(await db.scalars(select(AgentEvent).where(AgentEvent.session_id == session.id,
                AgentEvent.seq > after_seq).order_by(AgentEvent.seq).limit(100)))
            # Cursor describes delivered events, never skips a truncated page.
            return {"events": [v.payload for v in values],
                "cursor": values[-1].seq if values else min(after_seq, session.cursor)}

    async def stop_run(self, workspace_id, run_id, principal=None):
        async with self.factory.begin() as db:
            session, run = await self._lock_run(db, run_id, workspace_id)
            if run.phase == "media" and principal is not None and run.principal != principal:
                raise HTTPException(404, "创作运行不存在")
            return await self._stop(db, session, run)

    async def close_document(self, db, document_id, workspace_id=None):
        # The caller owns the Document lock and deletion transaction.
        query = select(AgentSession).where(AgentSession.document_id == document_id).with_for_update()
        if workspace_id is not None:
            query = query.where(AgentSession.workspace_id == workspace_id)
        sessions = list(await db.scalars(query.order_by(AgentSession.id)))
        for session in sessions:
            for run in await db.scalars(select(AgentRun).where(AgentRun.session_id == session.id,
                    AgentRun.status.in_([*ACTIVE, "canceling", "awaiting_confirm", "awaiting_user_final"]))
                    .order_by(AgentRun.id).with_for_update()):
                await self._stop(db, session, run, "DOCUMENT_DELETED")
            session.closed_at, session.document_id = now(), None
        # Source ids, call history and run asset pins remain available for audit.
