"""Session recovery and ordered replay. Redis notifications are hints, never history."""

import asyncio
import json
import uuid
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from starlette.responses import StreamingResponse

from backend.core.contracts import Contract
from backend.core.orm import (CoreRun, CoreSession, CoreSessionClient, CoreSessionDecision,
    CoreSessionEvent, CoreSessionMessage, CoreSessionNotification, CoreSessionArtifact, CoreAssetVersion)
from backend.core.events.store import db_now
from backend.core.asset_history import asset_head, compare, version_projection, versions
from backend.services.identity import Identity, authenticated


class StreamSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_SESSION_STREAM_")
    refresh_seconds: float = Field(default=1, gt=0)
    retry_ms: int = Field(default=2000, gt=0)
    replay_limit: int = Field(default=200, gt=0)
    replay_retention: int = Field(default=10000, gt=0)
    message_limit: int = Field(default=200, gt=0)
    run_limit: int = Field(default=30, gt=0)
    version_limit: int = Field(default=50, gt=0)
    notification_limit: int = Field(default=100, gt=0)
    client_lease_seconds: float = Field(default=90, gt=0)


async def scoped_session(tx, ident, identity, *, lock=False):
    row = await tx.get(CoreSession, ident, with_for_update=lock)
    if row is None or row.workspace_id != identity.workspace_id or row.user_id != identity.principal:
        raise HTTPException(404, "会话不存在")
    return row


async def snapshot(tx, session, settings=None):
    from backend.core.supervisor.service import progress
    settings = settings or StreamSettings()
    messages = list(await tx.scalars(select(CoreSessionMessage).where(
        CoreSessionMessage.session_id == session.id).order_by(CoreSessionMessage.sequence.desc())
        .limit(settings.message_limit)))
    runs = list(await tx.scalars(select(CoreRun).where(CoreRun.session_id == session.id,
        CoreRun.parent_run_id.is_(None)).order_by(CoreRun.created_at.desc()).limit(settings.run_limit)))
    decisions = list(await tx.scalars(select(CoreSessionDecision).where(
        CoreSessionDecision.session_id == session.id, CoreSessionDecision.superseded_by_id.is_(None))))
    notices = list(await tx.scalars(select(CoreSessionNotification).where(
        CoreSessionNotification.session_id == session.id, CoreSessionNotification.user_id == session.user_id,
        CoreSessionNotification.read_at.is_(None)).order_by(CoreSessionNotification.created_at.desc())
        .limit(settings.notification_limit)))
    return {"id": str(session.id), "cursor": session.event_cursor, "summary": session.summary,
        "summary_cursor": session.summary_cursor, "active_run_id": str(session.active_run_id) if session.active_run_id else None,
        "messages": [{"id": str(row.id), "sequence": row.sequence, "role": row.role,
            "content": {"text": (row.content or {}).get("text", "")},
            "processing_status": row.processing_status, "run_id": str(row.run_id) if row.run_id else None}
            for row in reversed(messages)],
        "decisions": [{"id": str(row.id), "value": row.value} for row in decisions],
        "notifications": [{"id": str(row.id), "kind": row.kind, "body": row.body} for row in notices],
        "runs": [await progress(tx, run) for run in reversed(runs)]}


def cursor_value(after: str | None, last: str | None):
    values = []
    for raw in (after, last):
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        if value < 0:
            return None
        values.append(value)
    return max(values) if values else None


def encode_event(name, data, cursor):
    return f"id: {cursor}\nevent: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class Heartbeat(Contract):
    client_id: str = Field(min_length=1, max_length=128)
    cursor: int = Field(default=0, ge=0)
    online: bool = True


def install(app):
    @app.get("/api/core/sessions/{session_id}/events")
    async def events(session_id: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        settings = StreamSettings()
        async with app.state.factory() as tx:
            await scoped_session(tx, session_id, identity)
        cursor = cursor_value(request.query_params.get("after"), request.headers.get("last-event-id"))

        async def stream():
            nonlocal cursor
            yield f"retry: {settings.retry_ms}\n\n"
            while not await request.is_disconnected():
                try:
                    current = await authenticated(request)
                    if current != identity:
                        return
                    async with app.state.factory.begin() as tx:
                        session = await scoped_session(tx, session_id, identity, lock=True)
                        head = session.event_cursor
                        if cursor is None or cursor > head or cursor < max(0, head - settings.replay_retention):
                            recovered = await snapshot(tx, session, settings)
                            cursor = head
                            rows = []
                        else:
                            recovered = None
                            rows = list(await tx.scalars(select(CoreSessionEvent).where(
                                CoreSessionEvent.session_id == session_id, CoreSessionEvent.sequence > cursor)
                                .order_by(CoreSessionEvent.sequence).limit(settings.replay_limit)))
                    if recovered is not None:
                        yield encode_event("snapshot", recovered, cursor)
                    for row in rows:
                        cursor = row.sequence
                        ids = {"event_id": str(row.event_id)}
                        for key in ("run_id", "task_id", "message_id", "causation_id", "version_id"):
                            value = getattr(row, key)
                            if value is not None:
                                ids[key] = str(value)
                        yield encode_event("session", ids, cursor)
                    if len(rows) == settings.replay_limit:
                        continue
                    yield ": keepalive\n\n"
                    await asyncio.sleep(settings.refresh_seconds)
                except HTTPException:
                    return
        return StreamingResponse(stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/core/sessions/{session_id}/clients")
    async def heartbeat(session_id: uuid.UUID, body: Heartbeat, identity: Identity = Depends(authenticated)):
        async with app.state.factory.begin() as tx:
            session = await scoped_session(tx, session_id, identity, lock=True)
            stamp = await db_now(tx)
            event_id = await tx.scalar(select(CoreSessionEvent.event_id).where(
                CoreSessionEvent.session_id == session.id, CoreSessionEvent.sequence == body.cursor))
            lease = stamp + timedelta(seconds=StreamSettings().client_lease_seconds) if body.online else stamp
            values = {"user_id": identity.principal, "heartbeat_at": stamp, "lease_until": lease,
                      "last_event_id": event_id, "metadata_json": {"cursor": min(body.cursor, session.event_cursor)}}
            await tx.execute(insert(CoreSessionClient).values(id=uuid.uuid4(), session_id=session.id,
                client_id=body.client_id, **values).on_conflict_do_update(
                    index_elements=[CoreSessionClient.session_id, CoreSessionClient.client_id], set_=values))
            return {"lease_until": lease.isoformat()}

    @app.get("/api/core/notifications")
    async def notifications(identity: Identity = Depends(authenticated)):
        async with app.state.factory() as tx:
            rows = list(await tx.scalars(select(CoreSessionNotification).join(CoreSession,
                CoreSession.id == CoreSessionNotification.session_id).where(
                    CoreSession.workspace_id == identity.workspace_id,
                    CoreSessionNotification.user_id == identity.principal)
                .order_by(CoreSessionNotification.created_at.desc()).limit(StreamSettings().notification_limit)))
            return {"notifications": [{"id": str(row.id), "session_id": str(row.session_id),
                "kind": row.kind, "body": row.body, "read_at": row.read_at.isoformat() if row.read_at else None} for row in rows]}

    @app.post("/api/core/notifications/{notification_id}/read")
    async def read_notification(notification_id: uuid.UUID, identity: Identity = Depends(authenticated)):
        async with app.state.factory.begin() as tx:
            row = await tx.get(CoreSessionNotification, notification_id)
            if row is None or row.user_id != identity.principal:
                raise HTTPException(404, "通知不存在")
            session = await scoped_session(tx, row.session_id, identity, lock=True)
            row = await tx.get(CoreSessionNotification, notification_id, with_for_update=True, populate_existing=True)
            if row is None:
                raise HTTPException(404, "通知不存在")
            was_read = row.read_at is not None
            row.read_at = row.read_at or await db_now(tx)
            if not was_read:
                from backend.core.sessions.store import append_event
                await append_event(tx, session, event_type="notification_read", payload={"notification_id": str(row.id)})
            return {"id": str(row.id), "read_at": row.read_at.isoformat()}

    @app.get("/api/core/sessions/{session_id}/artifacts")
    async def artifacts(session_id: uuid.UUID, before: int | None = None, identity: Identity = Depends(authenticated)):
        if before is not None and before <= 0:
            raise HTTPException(422, "产物游标无效")
        async with app.state.factory() as tx:
            await scoped_session(tx, session_id, identity)
            query = select(CoreSessionArtifact).where(CoreSessionArtifact.session_id == session_id)
            if before is not None:
                query = query.where(CoreSessionArtifact.ordinal < before)
            rows = list(await tx.scalars(query.order_by(CoreSessionArtifact.ordinal.desc()).limit(StreamSettings().version_limit)))
            result = []
            for row in rows:
                if row.core_asset_id is None or row.core_asset_version_id is None:
                    continue
                try:
                    await asset_head(tx, asset_id=row.core_asset_id, workspace_id=identity.workspace_id, user_id=identity.principal)
                except LookupError:
                    continue
                version = await tx.get(CoreAssetVersion, row.core_asset_version_id)
                if version is None:
                    continue
                result.append({"id": str(row.id), "ordinal": row.ordinal, "asset_id": str(row.stored_asset_id),
                    "core_asset_id": str(row.core_asset_id), "version": await version_projection(tx, version),
                    "canvas_node_id": row.canvas_node_id, "run_id": str(row.run_id) if row.run_id else None})
            return {"artifacts": result, "next_cursor": rows[-1].ordinal if rows else before}

    @app.get("/api/core/version-switches/{command_id}")
    async def switch_status(command_id: uuid.UUID, identity: Identity = Depends(authenticated)):
        from backend.core.version_switch_orm import VersionSwitchCommand
        async with app.state.factory() as tx:
            row = await tx.get(VersionSwitchCommand, command_id)
            if row is None or row.workspace_id != identity.workspace_id or row.user_id != identity.principal:
                raise HTTPException(404, "版本切换命令不存在")
            return {"id": str(row.id), "status": row.status, "error_code": row.error_code, "result": row.result}

    @app.get("/api/core/assets/{asset_id}/versions")
    async def history(asset_id: uuid.UUID, before: int | None = None, identity: Identity = Depends(authenticated)):
        try:
            async with app.state.factory() as tx:
                return {"versions": await versions(tx, asset_id=asset_id, before=before,
                    workspace_id=identity.workspace_id, user_id=identity.principal, limit=StreamSettings().version_limit)}
        except LookupError:
            raise HTTPException(404, "资产不存在") from None

    @app.get("/api/core/assets/{asset_id}/compare")
    async def comparison(asset_id: uuid.UUID, left: uuid.UUID, right: uuid.UUID, identity: Identity = Depends(authenticated)):
        try:
            async with app.state.factory() as tx:
                return await compare(tx, asset_id=asset_id, left=left, right=right,
                    workspace_id=identity.workspace_id, user_id=identity.principal)
        except LookupError:
            raise HTTPException(404, "资产版本不存在") from None
