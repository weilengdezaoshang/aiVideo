"""Authenticated HTTP boundary for persisted canvas Agent sessions.

SSE is a replay of committed database events. It never starts or retries work.
"""
import asyncio
import json
import uuid

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from backend.services.identity import Identity, authenticated


def encode_event(event: dict) -> str:
    return f"id: {event['seq']}\nevent: agent\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def install_agent_routes(app, read_json):
    @app.get("/api/agent/models")
    async def models(identity: Identity = Depends(authenticated)):
        configured = [route.public() for route in app.state.agent_routes]
        preferred = next((model['id'] for model in configured if model['provider'] == 'deepseek'), None)
        return {"models": configured, "defaultModelId": preferred or (configured[0]['id'] if configured else None)}

    @app.get("/api/documents/{ident}/agent")
    async def session(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        return await app.state.agent.snapshot(identity.workspace_id, ident,
            client_request_id=request.query_params.get("clientRequestId"))

    @app.post("/api/documents/{ident}/agent/runs")
    async def create(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        body = await read_json(request)
        route = next((item for item in app.state.agent_routes if item.id == body.get("modelId")), None)
        result = await app.state.agent.create_run(identity.workspace_id, ident, body, route,
            principal=identity.principal)
        return JSONResponse(result, status_code=200 if result.get("replayed") else 202)

    @app.get("/api/agent/runs/{ident}")
    async def run(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        return {"run": await app.state.agent.get_run(identity.workspace_id, ident)}

    @app.post("/api/agent/runs/{ident}/stop")
    async def stop(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        return {"run": await app.state.agent.stop_run(identity.workspace_id, ident)}

    @app.get("/api/documents/{ident}/agent/events")
    async def events(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        try:
            after = max(int(request.query_params.get("after", "0")),
                int(request.headers.get("last-event-id", "0")))
            if after < 0:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, "事件游标无效") from None
        # Authorize before streaming so missing/inaccessible documents return an HTTP error.
        initial = await app.state.agent.events(identity.workspace_id, ident, after_seq=after)

        async def stream():
            cursor = after
            current = initial
            yield "retry: 3000\n\n"
            try:
                while not await request.is_disconnected():
                    # An invalid cursor from another document cannot suppress this session forever.
                    if cursor > current['cursor']:
                        snapshot = await app.state.agent.snapshot(identity.workspace_id, ident)
                        cursor = snapshot['cursor']
                        yield "event: snapshot\ndata: " + json.dumps(snapshot, ensure_ascii=False) + "\n\n"
                    else:
                        for event in current.get('events', []):
                            if event['seq'] > cursor:
                                cursor = event['seq']
                                yield encode_event(event)
                    yield ": ping\n\n"
                    await asyncio.sleep(min(app.state.settings.sse_refresh_s, 2))
                    refreshed = await authenticated(request)
                    if refreshed.workspace_id != identity.workspace_id:
                        return
                    current = await app.state.agent.events(identity.workspace_id, ident, after_seq=cursor)
            except (HTTPException, SQLAlchemyError):
                return
        return StreamingResponse(stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
