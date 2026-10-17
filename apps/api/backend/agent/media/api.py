"""OIDC membership, CSRF and creator isolation apply to every Host operation."""
from __future__ import annotations

import asyncio
import json
import hmac
import uuid

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from backend.agent.api import encode_event
from backend.agent.media.catalog import HostError
from backend.agent.media.contracts import HostConfirm
from backend.agent.media.credits import account_locked, grant
from backend.services.identity import Identity, authenticated


def editor(identity):
    if identity.role not in {"admin", "owner", "editor", "member"}:
        raise HTTPException(403, "当前角色没有画布写入权限")


def install_host_routes(app, read_json):
    @app.exception_handler(HostError)
    async def host_error(_request, exc):
        return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status)

    async def validated(operation):
        try:
            return await operation
        except (ValidationError, ValueError):
            raise HostError("invalid_request", "创作请求字段不合法", status=400) from None

    @app.get("/api/host/capabilities")
    async def capabilities(identity: Identity = Depends(authenticated)):
        return app.state.host.catalog.public()

    @app.post("/api/host/turn")
    async def turn(request: Request, identity: Identity = Depends(authenticated)):
        editor(identity)
        result = await validated(app.state.host.create(identity.workspace_id, identity.principal, await read_json(request)))
        return JSONResponse(result, status_code=200 if result["replayed"] else 202)

    @app.get("/api/host/runs/{ident}")
    async def get(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        return await app.state.host.get(identity.workspace_id, identity.principal, ident)

    @app.get("/api/host/runs/{ident}/ledger")
    async def ledger(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        from sqlalchemy import select
        from backend.agent.orm import HostCreditLedger
        await app.state.host.get(identity.workspace_id, identity.principal, ident)
        async with app.state.factory() as db:
            rows = list(await db.scalars(select(HostCreditLedger).where(HostCreditLedger.run_id == ident).order_by(HostCreditLedger.created_at)))
            return {"entries": [{"reservationId": str(row.id), "stepId": str(row.step_id), "index": row.child_index,
                "attempt": row.attempt, "quoted": row.quoted, "charged": row.charged, "refunded": row.refunded,
                "state": row.state, "capabilityId": row.details["capabilityId"], "tier": row.details["tier"],
                "bindingVersion": row.details["bindingVersion"], "providerTaskId": row.details.get("providerTaskId"),
                "errorCode": row.details.get("errorCode")} for row in rows]}

    @app.post("/api/host/runs/{ident}/confirm")
    async def confirm(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        editor(identity)
        try:
            body = HostConfirm.model_validate(await read_json(request))
        except ValidationError:
            raise HostError("invalid_request", "确认请求字段不合法", status=400) from None
        return await app.state.host.confirm(identity.workspace_id, identity.principal, ident,
                                            body.clientRequestId, body.approved)

    @app.post("/api/host/runs/{ident}/canvas-result")
    async def canvas_result(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        editor(identity)
        return await validated(app.state.host.complete_canvas_action(
            identity.workspace_id, identity.principal, ident, await read_json(request)))

    @app.post("/api/host/runs/{ident}/confirm-final")
    async def final(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        editor(identity)
        result = await validated(app.state.host.final(identity.workspace_id, identity.principal, ident, await read_json(request)))
        return JSONResponse(result, status_code=200 if result["replayed"] else 202)

    @app.post("/api/host/runs/{ident}/retry")
    async def retry(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        editor(identity)
        return await validated(app.state.host.retry(identity.workspace_id, identity.principal, ident, await read_json(request)))

    @app.post("/api/host/runs/{ident}/cancel")
    async def cancel(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        editor(identity)
        return await app.state.host.cancel(identity.workspace_id, identity.principal, ident)

    @app.get("/api/documents/{ident}/host")
    async def snapshot(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        return await app.state.host.snapshot(identity.workspace_id, identity.principal, ident,
                                            request.query_params.get("clientRequestId"))

    @app.get("/api/host/credits")
    async def balance(identity: Identity = Depends(authenticated)):
        async with app.state.factory.begin() as db:
            account = await account_locked(db, identity.workspace_id)
            return {"available": account.available}

    @app.post("/api/host/credits/grant")
    async def credit(request: Request, identity: Identity = Depends(authenticated)):
        if identity.role not in {"admin", "owner"}:
            raise HTTPException(403, "只有工作区管理员可以授予积分")
        # Tenant administrators cannot mint a balance that spends platform provider funds.
        # A separate platform billing credential is required; it never enters browser flows.
        token = app.state.settings.host_credit_grant_token
        if token is None:
            raise HTTPException(503, "积分授予由平台计费服务管理，当前接口未配置")
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token.get_secret_value()).encode()):
            raise HTTPException(403, "需要平台计费授权")
        body = await read_json(request)
        if (set(body) != {"clientRequestId", "amount"} or not isinstance(body.get("clientRequestId"), str)
                or not 1 <= len(body["clientRequestId"]) <= 128):
            raise HostError("invalid_request", "积分授予请求无效", status=400)
        async with app.state.factory.begin() as db:
            return await grant(db, identity.workspace_id, body["clientRequestId"], body["amount"], identity.principal)

    @app.get("/api/documents/{ident}/host/events")
    async def events(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        try:
            after = max(int(request.query_params.get("after", "0")), int(request.headers.get("last-event-id", "0")))
            if after < 0:
                raise ValueError()
        except ValueError:
            raise HostError("invalid_cursor", "事件游标无效", status=400) from None
        current = await app.state.host.events(identity.workspace_id, identity.principal, ident, after)

        async def stream():
            cursor = after
            page = current
            yield "retry: 3000\n\n"
            try:
                while not await request.is_disconnected():
                    if cursor > page["cursor"]:
                        snapshot = await app.state.host.snapshot(identity.workspace_id, identity.principal, ident)
                        cursor = snapshot["cursor"]
                        yield "event: snapshot\ndata: " + json.dumps(snapshot, ensure_ascii=False) + "\n\n"
                    else:
                        for event in page["events"]:
                            if event["seq"] > cursor:
                                cursor = event["seq"]
                                yield encode_event(event)
                    yield ": ping\n\n"
                    await asyncio.sleep(min(app.state.settings.sse_refresh_s, 2))
                    refreshed = await authenticated(request)
                    if refreshed.workspace_id != identity.workspace_id or refreshed.principal != identity.principal:
                        return
                    page = await app.state.host.events(identity.workspace_id, identity.principal, ident, cursor)
            except (HTTPException, SQLAlchemyError):
                return
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
