"""生成受理用例(补充要求 §五.1):任务、幂等记录与 Outbox 事件在同一事务提交。

API 层只有在本用例成功返回后才能返回 202(§五.1);
数据库不可用/持久化失败时异常上抛,不得声称任务已受理(§五.3)。
"""

import uuid

from backend.errors import ConflictError
from backend.infrastructure.database import DatabaseSettings


async def accept_generation(
    settings: DatabaseSettings,
    *,
    workspace_id: uuid.UUID,
    request_id: str,
    request_hash: str,
    params: dict,
    kind: str = "image",
) -> dict:
    """受理一次生成请求;同键同参数幂等重放,同键不同参数冲突(409)。"""
    from backend.infrastructure.uow import AsyncUnitOfWork

    async with AsyncUnitOfWork(settings) as uow:
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
        job = await uow.jobs.create(workspace_id=workspace_id, kind=kind, params=params)
        await uow.requests.register(
            workspace_id, request_id, request_hash, params, job.id
        )
        await uow.outbox.enqueue(
            aggregate_type="job",
            aggregate_id=str(job.id),
            event_type="job.created",
            payload={"jobId": str(job.id)},
        )
        await uow.commit()
        return {"jobId": job.id, "replayed": False}
