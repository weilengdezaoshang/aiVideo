"""生成受理用例(补充要求 §五.1):任务、幂等记录与 Outbox 事件在同一事务提交。

API 层只有在本用例成功返回后才能返回 202(§五.1);
数据库不可用/持久化失败时异常上抛,不得声称任务已受理(§五.3)。
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from backend.errors import ConflictError, QueueFullError
from backend.infrastructure.database import DatabaseSettings
from backend.infrastructure.orm import Asset, AssetReference, Document, Job, Outbox, RequestTombstone
from backend.providers.policy import ProviderPolicy


async def accept_generation(
    settings: DatabaseSettings,
    *,
    workspace_id: uuid.UUID,
    request_id: str,
    request_hash: str,
    params: dict,
    kind: str = "image",
    provider_snapshot: dict | None = None,
    policy: ProviderPolicy | None = None,
    principal: str | None = None,
    child_overrides: list[dict] | None = None,
) -> dict:
    """受理一次生成请求;同键同参数幂等重放,同键不同参数冲突(409)。"""
    from backend.infrastructure.uow import AsyncUnitOfWork

    if provider_snapshot and any("key" in key.lower() or "secret" in key.lower()
                                 or "password" in key.lower() for key in provider_snapshot):
        raise ValueError("Provider snapshot must contain credential references, not secrets")
    async with AsyncUnitOfWork(settings) as uow:
        # Global admission serialization also closes the concurrent idempotency race.
        await uow.session.execute(text("SELECT pg_advisory_xact_lock(72140901)"))
        await uow.workspaces.ensure(workspace_id)
        existing = await uow.requests.find(workspace_id, request_id)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ConflictError(
                    "同一受理标识已用于不同参数",
                    details={"requestId": request_id, "jobId": str(existing.job_id)},
                )
            await uow.commit()
            return {"jobId": existing.job_id, "replayed": True}
        if await uow.session.get(RequestTombstone, (workspace_id, request_id)):
            raise ConflictError("该次生成已取消")
        limits = policy or ProviderPolicy()
        count = int(params.get("batchCount", 1))
        if not 1 <= count <= 16 or kind not in {"image", "video", "export"}:
            raise ValueError("Invalid task kind or batch count")
        if child_overrides is not None and (count <= 1 or len(child_overrides) != count):
            raise ValueError("Invalid child variants")
        backlog = await uow.session.scalar(select(func.count()).select_from(Outbox).where(
            Outbox.status.in_(["pending", "publishing", "failed"])))
        if backlog + count > limits.outbox_capacity:
            raise QueueFullError("可靠投递积压超限，请稍后重试")
        active = Job.status.in_(["queued", "running", "unknown"]) & (Job.kind != "batch")
        total = await uow.session.scalar(select(func.count()).select_from(Job).where(active))
        scoped = await uow.session.scalar(select(func.count()).select_from(Job).where(
            active, Job.workspace_id == workspace_id))
        if total + count > limits.global_capacity or scoped + count > limits.workspace_capacity:
            raise QueueFullError("任务队列已满，请稍后重试")
        if principal:
            recent = await uow.session.scalar(select(func.count()).select_from(Job).where(
                Job.params["submittedBy"].astext == principal, Job.kind != "batch",
                Job.created_at >= datetime.now(timezone.utc) - timedelta(seconds=60)))
            if recent + count > limits.user_per_minute:
                raise QueueFullError("新增任务过于频繁，请稍后重试")
        document_id = uuid.UUID(params["documentId"]) if params.get("documentId") else None
        if document_id:
            doc = await uow.session.get(Document, document_id, with_for_update=True)
            if doc is None or doc.workspace_id != workspace_id:
                raise ValueError("Document is unavailable")
        refs = [uuid.UUID(value) for value in params.get("referenceAssetIds", [])]
        for ident in sorted(set(refs)):
            asset = await uow.session.get(Asset, ident, with_for_update=True)
            if asset is None or asset.workspace_id != workspace_id:
                raise ValueError("Reference asset is unavailable")
        stored = {**params, **({"submittedBy": principal} if principal else {})}
        job = await uow.jobs.create(workspace_id=workspace_id, kind="batch" if count > 1 else kind, params=stored)
        job.document_id = document_id
        job.provider_snapshot = dict(provider_snapshot or {})
        if kind == "export":
            job.phase = "export"
        job.queue_deadline = datetime.now(timezone.utc) + timedelta(seconds=limits.queue_s)
        await uow.requests.register(
            workspace_id, request_id, request_hash, params, job.id
        )
        children = [job]
        if count > 1:
            children = []
            for index in range(count):
                child_params = {**stored, "batchCount": 1}
                if child_overrides:
                    child_params.update(child_overrides[index])
                    child_params["groupId"] = str(job.id)
                if child_params.get("seed", -1) >= 0:
                    child_params["seed"] = (child_params["seed"] + index) % (2**31)
                child = await uow.jobs.create(workspace_id=workspace_id, kind=kind, params=child_params)
                child.parent_id, child.document_id = job.id, document_id
                child.provider_snapshot, child.queue_deadline = dict(job.provider_snapshot), job.queue_deadline
                children.append(child)
        for child in children:
            for ident in set(refs):
                uow.session.add(AssetReference(asset_id=ident, owner_type="job", owner_id=str(child.id)))
            await uow.outbox.enqueue(aggregate_type="job", aggregate_id=str(child.id),
                event_type="execution.due" if provider_snapshot else "job.created",
                payload={"jobId": str(child.id), "traceId": stored.get("traceId", "")})
        await uow.commit()
        return {"jobId": job.id, "replayed": False}
