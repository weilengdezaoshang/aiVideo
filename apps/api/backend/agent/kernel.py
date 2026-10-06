"""Durable execution kernel: leases, fenced epochs, one effect per tick, recovery, stop.

Lock order is Document (admission/deletion only), Session, Run. Network and
filesystem work never hold a transaction. A sending model call is never replayed.
The Kernel owns *how* a Run advances safely; a loop policy (see legacy_loop.py)
owns *what* the next model request and the interpretation of its answer are.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import or_, select

from backend.agent.legacy_loop import LegacyLoop
from backend.agent.orm import AgentEvent, AgentModelCall, AgentRun, AgentSession, AgentToolCall
from backend.agent.util import encoded, error, now, public_error, stamp
from backend.infrastructure.orm import Outbox

ACTIVE = ("queued", "running")
MAX_MODEL_CALLS = 8
MAX_TOOL_CALLS = 12
TOOL_TIMEOUT_S = 15
TOOL_HEARTBEAT_S = 5


@dataclass(frozen=True)
class Limits:
    max_model_calls: int
    max_tool_calls: int
    tool_timeout_s: float
    tool_heartbeat_s: float


class Kernel:
    def __init__(self, factory, loop=None):
        self.factory = factory
        self.loop = loop or LegacyLoop()

    def limits(self) -> Limits:
        return Limits(MAX_MODEL_CALLS, MAX_TOOL_CALLS, TOOL_TIMEOUT_S, TOOL_HEARTBEAT_S)

    async def _lock_run(self, db, ident, workspace=None):
        session_id = await db.scalar(select(AgentRun.session_id).where(AgentRun.id == ident))
        if session_id is None:
            raise HTTPException(404, "Agent 运行不存在")
        session = await db.get(AgentSession, session_id, with_for_update=True)
        if workspace is not None and session.workspace_id != workspace:
            raise HTTPException(404, "Agent 运行不存在")
        run = await db.get(AgentRun, ident, with_for_update=True)
        return session, run

    async def _project(self, db, run):
        models = list(await db.scalars(select(AgentModelCall).where(AgentModelCall.run_id == run.id)
            .order_by(AgentModelCall.ordinal)))
        tools = list(await db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id)
            .order_by(AgentToolCall.ordinal)))
        return {"id": str(run.id), "sessionId": str(run.session_id),
            "clientRequestId": run.client_request_id, "status": run.status, "phase": run.phase,
            "message": run.message, "reply": run.reply, "error": public_error(run.error),
            "errorCode": run.error.get("code") if run.error else None,
            "modelId": run.route.get("id"), "baseRevision": run.snapshot["revision"],
            "selectedObjectIds": run.snapshot.get("selectedObjectIds", []),
            "stateVersion": run.state_version, "modelCallCount": run.model_call_count,
            "toolCallCount": run.tool_call_count, "createdAt": stamp(run.created_at),
            "updatedAt": stamp(run.updated_at),
            "modelCalls": [{"id": str(c.id), "ordinal": c.ordinal, "status": c.status,
                "usage": c.usage, "error": public_error(c.error), "createdAt": stamp(c.created_at),
                "finishedAt": stamp(c.finished_at)} for c in models],
            "toolCalls": [{"id": str(c.id), "modelCallId": str(c.model_call_id),
                "toolCallId": c.tool_call_id, "name": c.name, "arguments": c.arguments,
                "status": c.status, "output": c.output, "error": public_error(c.error),
                "createdAt": stamp(c.created_at), "finishedAt": stamp(c.finished_at)} for c in tools]}

    async def _event(self, db, session, run, kind):
        run.state_version += 1
        run.updated_at = now()
        session.cursor += 1
        await db.flush()
        value = await self._project(db, run)
        db.add(AgentEvent(session_id=session.id, seq=session.cursor, run_id=run.id,
            type=kind, payload={"seq": session.cursor, "runId": str(run.id), "type": kind, "run": value}))
        return value

    def _wake(self, db, run):
        run.next_run_at = now()
        run.dispatched_until = now() + timedelta(seconds=30)
        db.add(Outbox(aggregate_type="agent", aggregate_id=str(run.id), event_type="agent.due",
            payload={"runId": str(run.id)}))

    def _finish(self, run, status, reason=None):
        run.status, run.error = status, reason
        run.lease_owner = run.lease_until = run.next_run_at = run.dispatched_until = None
        run.execution_epoch += 1

    async def _stop(self, db, session, run, code="STOPPED"):
        if run.status not in ACTIVE:
            return await self._project(db, run)
        run.stop_requested_at = now()
        self._finish(run, "stopped", error(code, "运行已停止"))
        for call in await db.scalars(select(AgentModelCall).where(AgentModelCall.run_id == run.id,
                AgentModelCall.status == "sending")):
            call.status, call.error = "unknown", error("MODEL_RESPONSE_UNKNOWN", "停止时模型请求已在途")
        for call in await db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id,
                AgentToolCall.status.in_(["pending", "running"]))):
            call.status, call.finished_at = "cancelled", now()
        return await self._event(db, session, run, "run.stopped")

    async def _prepare(self, run_id, owner):
        from backend.agent.config import AgentRoute
        async with self.factory.begin() as db:
            session, run = await self._lock_run(db, run_id)
            if session.closed_at or run.status not in ACTIVE or run.stop_requested_at:
                return None
            if run.lease_owner is not None:
                return None  # Only recover_due may fence an expired owner.
            route = AgentRoute.from_snapshot(run.route)
            run.execution_epoch += 1
            run.lease_owner, run.lease_until = owner, now() + timedelta(seconds=route.timeout_s + 30)
            run.status, run.dispatched_until = "running", None
            if run.phase == "model":
                if run.model_call_count >= self.limits().max_model_calls:
                    self._finish(run, "limit_reached", error("MODEL_CALL_LIMIT", "已达到模型调用上限"))
                    await self._event(db, session, run, "run.limit_reached")
                    return None
                messages = await self.loop.messages(db, run)
                if len(encoded(messages).encode()) > route.context_max_bytes:
                    self._finish(run, "limit_reached", error("CONTEXT_LIMIT", "上下文已超过配置上限"))
                    await self._event(db, session, run, "run.limit_reached")
                    return None
                run.model_call_count += 1
                call = AgentModelCall(run_id=run.id, ordinal=run.model_call_count, status="sending",
                    execution_epoch=run.execution_epoch, messages=messages, usage={})
                db.add(call)
                await self._event(db, session, run, "model.started")
                return {"kind": "model", "callId": call.id, "epoch": run.execution_epoch,
                    "route": route, "messages": messages}
            call = await db.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run.id,
                AgentToolCall.status == "pending").order_by(AgentToolCall.ordinal).limit(1))
            if call is None:
                run.phase, run.lease_owner, run.lease_until = "model", None, None
                self._wake(db, run)
                await self._event(db, session, run, "tools.completed")
                return None
            call.status, call.execution_epoch = "running", run.execution_epoch
            await self._event(db, session, run, "tool.started")
            return {"kind": "tool", "callId": call.id, "epoch": run.execution_epoch,
                "name": call.name, "arguments": call.arguments, "snapshot": run.snapshot,
                "assets": run.assets, "route": route}

    def _owned(self, session, run, owner, claim):
        return (not session.closed_at and run.status in ACTIVE and not run.stop_requested_at
            and run.lease_owner == owner and run.execution_epoch == claim["epoch"]
            and run.lease_until is not None and run.lease_until > now())

    async def _invoke_model(self, run_id, claim, model_client, schemas):
        from backend.agent.providers import ModelCallError

        async def watch_stop():
            while True:
                await asyncio.sleep(.5)
                async with self.factory() as db:
                    row = (await db.execute(select(AgentRun.status, AgentRun.execution_epoch,
                        AgentRun.stop_requested_at).where(AgentRun.id == run_id))).one_or_none()
                if row is None or row.status not in ACTIVE or row.stop_requested_at or row.execution_epoch != claim["epoch"]:
                    return

        request = asyncio.create_task(model_client.complete(claim["route"], claim["messages"], schemas))
        watcher = asyncio.create_task(watch_stop())
        try:
            async with asyncio.timeout(claim["route"].timeout_s):
                done, _ = await asyncio.wait((request, watcher), return_when=asyncio.FIRST_COMPLETED)
                if request in done:
                    return await request
                await watcher  # A failed coordination read also prevents further work.
                request.cancel()
                try:
                    # Some transports finish while cancellation is in flight. Keep
                    # their receipt; advance() fences it from further execution.
                    return await request
                except asyncio.CancelledError:
                    pass
                raise ModelCallError("MODEL_RESPONSE_UNKNOWN", "运行已停止或执行权已失效，模型请求已取消等待", unknown=True)
        except TimeoutError:
            request.cancel()
            receipts = await asyncio.gather(request, return_exceptions=True)
            if not isinstance(receipts[0], BaseException):
                return receipts[0]
            raise ModelCallError("MODEL_RESPONSE_UNKNOWN", "模型请求超时，未自动重试", unknown=True) from None
        finally:
            for task in (request, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(request, watcher, return_exceptions=True)

    async def _invoke_tool(self, run_id, owner, tools, claim, media_root):
        # A timeout cannot kill a native/Pillow thread. Keep the phase occupied
        # until the actual invocation exits, including cancellation of this task.
        task = asyncio.create_task(asyncio.to_thread(tools.execute_tool, claim["name"], claim["arguments"],
            claim["snapshot"], claim["assets"], Path(media_root),
            max_output_bytes=claim["route"].tool_output_max_bytes))

        async def heartbeat():
            while not task.done():
                await asyncio.sleep(self.limits().tool_heartbeat_s)
                async with self.factory.begin() as db:
                    session, run = await self._lock_run(db, run_id)
                    if not self._owned(session, run, owner, claim):
                        return
                    run.lease_until = now() + timedelta(seconds=30)

        keep_alive = asyncio.create_task(heartbeat())
        try:
            async with asyncio.timeout(self.limits().tool_timeout_s):
                return await asyncio.shield(task)
        except (TimeoutError, asyncio.CancelledError):
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)
            raise
        finally:
            keep_alive.cancel()
            await asyncio.gather(keep_alive, return_exceptions=True)

    async def advance(self, run_id, owner, model_client, tools=None, media_root=None):
        from backend.agent import tools as default_tools
        from backend.agent.providers import ModelCallError
        tools = tools or default_tools
        claim = await self._prepare(run_id, owner)
        if claim is None:
            return {"skipped": True}
        result, failure, failure_receipt = None, None, None
        try:
            if claim["kind"] == "model":
                result = await self._invoke_model(run_id, claim, model_client, tools.tool_schemas())
            else:
                result = await self._invoke_tool(run_id, owner, tools, claim, media_root)
        except asyncio.CancelledError:
            # Persisted sending marker is reconciled by the scanner; never erase it.
            raise
        except ModelCallError as exc:
            failure = {"code": exc.code, "message": str(exc), "unknown": exc.unknown}
            failure_receipt = {"usage": exc.usage, "raw_response": exc.raw_response,
                "request_id": exc.request_id}
        except Exception as exc:
            if claim["kind"] == "tool" and hasattr(exc, "code"):
                failure = error(exc.code, str(exc))
            else:
                failure = {"code": "MODEL_RESPONSE_UNKNOWN" if claim["kind"] == "model" else "TOOL_FAILED",
                    "message": "模型请求结果未知" if claim["kind"] == "model" else "工具执行失败",
                    "unknown": claim["kind"] == "model"}
        async with self.factory.begin() as db:
            session, run = await self._lock_run(db, run_id)
            valid = self._owned(session, run, owner, claim)
            if claim["kind"] == "model":
                call = await db.get(AgentModelCall, claim["callId"])
                if result is not None:
                    # Late receipts remain useful audit facts; they never restart a Run.
                    call.response, call.raw_response = result.message(), result.raw_response
                    call.usage, call.request_id = result.usage, result.request_id
                    call.finished_at = now()
                    call.status = "completed" if valid else "late"
                else:
                    call.status, call.error, call.finished_at = (
                        "unknown" if failure.get("unknown") else "failed", failure, now())
                    if failure_receipt:
                        call.usage = failure_receipt["usage"]
                        call.raw_response = failure_receipt["raw_response"]
                        call.request_id = failure_receipt["request_id"]
                if not valid:
                    if run.status in ACTIVE:
                        self._finish(run, "needs_attention", error("MODEL_RESPONSE_UNKNOWN",
                            "模型响应在执行权失效后到达，未继续执行"))
                    await self._event(db, session, run, "model.late_receipt")
                    return {"late": True}
                if failure:
                    status = ("needs_attention" if failure.get("unknown") else
                        "limit_reached" if failure["code"] == "CONTEXT_LIMIT" else "failed")
                    self._finish(run, status, failure)
                    await self._event(db, session, run, "run." + run.status)
                    return {"status": run.status}
                outcome = self.loop.after_model(run, result, self.limits())
                if outcome.kind == "end":
                    if outcome.reply is not None:
                        run.reply = outcome.reply
                    self._finish(run, outcome.status, outcome.error)
                    await self._event(db, session, run, "run." + run.status)
                    return {"status": run.status}
                for request in outcome.calls:
                    run.tool_call_count += 1
                    db.add(AgentToolCall(run_id=run.id, model_call_id=call.id,
                        tool_call_id=request["id"], ordinal=run.tool_call_count,
                        name=request["name"], arguments=request["arguments"], status="pending"))
                run.phase = "tools"
            else:
                call = await db.get(AgentToolCall, claim["callId"])
                # A stale retry must not overwrite a result from a newer owner.
                if call.execution_epoch != claim["epoch"]:
                    return {"late": True}
                call.output, call.error, call.finished_at = result, failure, now()
                call.status = ("failed" if failure else "completed") if valid else "late"
                if not valid:
                    if run.status in ACTIVE:
                        call.status, call.execution_epoch = "pending", None
                        call.output = call.error = call.finished_at = None
                        run.execution_epoch += 1
                        run.lease_owner = run.lease_until = None
                        self._wake(db, run)
                    await self._event(db, session, run, "tool.late_receipt")
                    return {"late": True}
                await db.flush()
                pending = await db.scalar(select(AgentToolCall.id).where(AgentToolCall.run_id == run.id,
                    AgentToolCall.status == "pending").limit(1))
                run.phase = "tools" if pending else "model"
            run.lease_owner = run.lease_until = None
            self._wake(db, run)
            await self._event(db, session, run, "model.completed" if claim["kind"] == "model" else "tool.completed")
            return {"advanced": True, "phase": run.phase}

    async def recover_due(self, limit=100):
        # Discover without Run locks; all mutations below acquire Session then Run.
        async with self.factory() as db:
            ready_without_owner = (AgentRun.lease_owner.is_(None)
                & or_(AgentRun.dispatched_until.is_(None), AgentRun.dispatched_until <= now())
                & or_(AgentRun.next_run_at.is_(None), AgentRun.next_run_at <= now()))
            ids = list(await db.scalars(select(AgentRun.id).where(AgentRun.status.in_(ACTIVE),
                or_(AgentRun.lease_until <= now(), ready_without_owner))
                .order_by(AgentRun.created_at, AgentRun.id).limit(limit)))
        dispatched = 0
        for ident in ids:
            async with self.factory.begin() as db:
                session, run = await self._lock_run(db, ident)
                if run.status not in ACTIVE or session.closed_at:
                    continue
                if run.lease_owner is not None:
                    if run.lease_until is not None and run.lease_until > now():
                        continue
                    run.execution_epoch += 1
                    sending = list(await db.scalars(select(AgentModelCall).where(AgentModelCall.run_id == run.id,
                        AgentModelCall.status == "sending")))
                    if sending:
                        reason = error("MODEL_RESPONSE_UNKNOWN", "模型请求在 Worker 失联前已开始，未自动重试")
                        for call in sending:
                            call.status, call.error = "unknown", reason
                        self._finish(run, "needs_attention", reason)
                        await self._event(db, session, run, "run.needs_attention")
                        continue
                    for call in await db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id,
                            AgentToolCall.status == "running")):
                        call.status, call.execution_epoch = "pending", None
                    run.lease_owner = run.lease_until = None
                    run.dispatched_until = None
                    await self._event(db, session, run, "run.recovered")
                if run.dispatched_until is not None and run.dispatched_until > now():
                    continue
                if run.next_run_at is not None and run.next_run_at > now():
                    continue
                self._wake(db, run)
                dispatched += 1
        return dispatched
