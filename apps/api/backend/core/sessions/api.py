"""Session resolve and message intake HTTP surface."""
import uuid
from typing import Literal
from fastapi import Depends, HTTPException
from pydantic import Field

from backend.core.contracts import Contract, CoreSettings
from backend.core.orm import CoreSession
from backend.services.identity import Identity, authenticated
from .contracts import SessionSettings
from .intake import enqueue_message
from .resolve import resolve_session


class OpenSession(Contract):
    document_id: uuid.UUID
    language: str | None = None


class PostMessage(Contract):
    client_id: str = Field(min_length=1, max_length=128)
    client_message_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1)
    selected_assets: list[uuid.UUID] = Field(default_factory=list)
    selected_node_ids: list[str] = Field(default_factory=list)
    intent: str | None = None
    related_run_id: uuid.UUID | None = None
    correction_of_run_id: uuid.UUID | None = None
    viewport: dict | None = None
    language: str | None = None
    skill_id: uuid.UUID | None = None
    credits_budget: int = Field(default=0, ge=0)
    token_budget: int = Field(default_factory=lambda: CoreSettings().tokens_limit, ge=0)
    max_thought_rounds: int = Field(default_factory=lambda: CoreSettings().max_thought_rounds, gt=0)
    mode: Literal["auto", "plan"] = "auto"


def _route_snapshot(app, identity):
    from backend.core.supervisor.api import _route_for
    from backend.core.supervisor.wiring import route_snapshot
    return route_snapshot(_route_for(app, identity))


def install(app):
    @app.post("/api/core/sessions", status_code=201)
    async def open_session(body: OpenSession, identity: Identity = Depends(authenticated)):
        try:
            async with app.state.factory.begin() as tx:
                session, _doc = await resolve_session(tx, workspace_id=identity.workspace_id,
                    user_id=identity.principal, document_id=body.document_id,
                    language=body.language or "zh-CN")
                from backend.core.session_stream import snapshot
                return await snapshot(tx, session)
        except LookupError:
            raise HTTPException(404, "画布不存在") from None

    @app.post("/api/core/sessions/{session_id}/messages", status_code=202)
    async def post_message(session_id: uuid.UUID, body: PostMessage, identity: Identity = Depends(authenticated)):
        try:
            async with app.state.factory.begin() as tx:
                session = await tx.get(CoreSession, session_id, with_for_update=True)
                if session is None or session.workspace_id != identity.workspace_id or session.user_id != identity.principal:
                    raise LookupError("session_missing")
                content = {"text": body.message, "selected_assets": [str(item) for item in body.selected_assets],
                    "node_ids": body.selected_node_ids,
                    "budget": body.credits_budget, "tokens": body.token_budget, "rounds": body.max_thought_rounds,
                    "mode": body.mode}
                if body.intent in {"batch", "for_each"}:
                    content["action"] = body.intent
                if body.viewport is not None:
                    content["viewport"] = body.viewport
                if body.language:
                    content["language"] = body.language
                if body.skill_id is not None:
                    content["skill_id"] = str(body.skill_id)
                result = await enqueue_message(tx, session, client_id=body.client_id,
                    client_message_id=body.client_message_id, content=content, intent=body.intent,
                    related_run_id=body.related_run_id, correction_of_run_id=body.correction_of_run_id,
                    settings=SessionSettings(), server_metadata={"_agent_route": _route_snapshot(app, identity)})
                return {"message_id": str(result.message_id), "sequence": result.sequence,
                    "dispatch_at": result.dispatch_at.isoformat() if result.dispatch_at else None,
                    "run_id": str(result.run_id) if result.run_id else None, "replayed": result.replayed}
        except LookupError:
            raise HTTPException(404, "会话不存在") from None
        except ValueError as exc:
            code = 409 if str(exc) in {"request_key_conflict", "session_queue_limit"} else 400
            raise HTTPException(code, str(exc)) from None

    @app.get("/api/core/sessions/{session_id}")
    async def get_session(session_id: uuid.UUID, identity: Identity = Depends(authenticated)):
        from backend.core.session_stream import scoped_session, snapshot
        async with app.state.factory.begin() as tx:
            session = await scoped_session(tx, session_id, identity, lock=True)
            return await snapshot(tx, session)
