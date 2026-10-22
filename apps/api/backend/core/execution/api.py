"""Authenticated cost/cancellation queries and signed provider callback ingress."""
import hashlib
import hmac
import json
import os
import uuid

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from backend.agent.orm import HostCreditLedger
from backend.core.orm import CoreRun, CoreTask
from backend.services.identity import Identity, authenticated
from .contracts import ProviderResult, WorkerSettings


def install(app):
    async def scoped(tx, run_id, identity):
        row = await tx.get(CoreRun, run_id)
        if row is None or row.workspace_id != identity.workspace_id or row.user_id != identity.principal:
            raise HTTPException(404, "Run 不存在")
        return row

    @app.get("/api/core/runs/{run_id}/costs")
    async def costs(run_id: uuid.UUID, identity: Identity = Depends(authenticated)):
        async with app.state.factory() as tx:
            run = await scoped(tx, run_id, identity)
            tasks = (await tx.scalars(select(CoreTask).where(CoreTask.run_id == run.id).order_by(CoreTask.created_at, CoreTask.id))).all()
            ledgers = {r.core_task_id: r for r in await tx.scalars(select(HostCreditLedger).where(HostCreditLedger.core_run_id == run.id))}
            return {"run_id": str(run.id), "actual_credits": run.credits_spent, "entries": [
                {"task_id": str(t.id), "step": t.task_key, "status": t.status,
                 "reserved": ledgers[t.id].quoted if t.id in ledgers else 0,
                 "actual": t.actual_credits, "refunded": ledgers[t.id].refunded if t.id in ledgers else 0,
                 "settlement": ledgers[t.id].state if t.id in ledgers else "not_reserved",
                 "retry_count": t.retry_count} for t in tasks]}

    @app.post("/api/core/tasks/{task_id}/cancel")
    async def cancel(task_id: uuid.UUID, identity: Identity = Depends(authenticated)):
        async with app.state.factory() as tx:
            task = await tx.get(CoreTask, task_id)
            if task is None:
                raise HTTPException(404, "Task 不存在")
            await scoped(tx, task.run_id, identity)
            job_id = task.job_id
        if job_id is None:
            raise HTTPException(409, "Task 尚未进入执行队列")
        from .engine import Worker
        worker = Worker(app.state.factory, app.state.settings.media_root, None)
        return {"accepted": await worker.cancel(job_id)}

    @app.post("/api/core/provider-callbacks/{job_id}")
    async def callback(job_id: uuid.UUID, request: Request):
        key = os.environ.get("AIVERO_EXECUTION_CALLBACK_SECRET", "")
        if not key:
            raise HTTPException(503, "回调尚未配置")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > WorkerSettings().callback_max_bytes:
                raise HTTPException(413, "回调过大")
        # Include the path binding: a signed result cannot be redirected to another job.
        digest = hmac.new(key.encode(), str(job_id).encode() + b"\n" + bytes(data), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, request.headers.get("x-provider-signature", "")):
            raise HTTPException(403, "回调签名无效")
        try:
            body = json.loads(data)
            if not isinstance(body, dict) or not isinstance(body.get("provider_ref"), str):
                raise ValueError
            result = ProviderResult(**body["result"])
            if result.status not in {"pending", "succeeded", "failed", "cancelled"} or type(result.actual_credits) is not int or result.actual_credits < 0 or result.data is not None:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, "回调格式无效") from None
        from .engine import Worker
        worker = Worker(app.state.factory, app.state.settings.media_root, None)
        accepted = await worker.callback(job_id, body["provider_ref"], result)
        if not accepted:
            raise HTTPException(503, "回调等待提交记录确认，请重试")
        return {"accepted": True}
