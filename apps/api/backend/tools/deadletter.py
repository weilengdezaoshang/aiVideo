"""死信恢复工具(补充要求 §九.8-11):列表/重放,审计留痕,重放前检查业务状态。

- 重放前检查当前业务状态:任务已终态的事件拒绝重放(§九.9);
- 重放重置 attempts 并回到 pending,走正常投递与消费幂等(§五.6);
- 每次操作记录操作者/原因/原任务/动作/结果(§九.10),写入审计 JSONL;
- 不提供一键批量重新付费执行(§九.11),按事件逐条处理。
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from backend.infrastructure.database import DatabaseSettings

DEFAULT_AUDIT_PATH = "data/deadletter-audit.jsonl"


def list_dead(settings: DatabaseSettings, limit: int = 20) -> list[dict]:
    """列出死信语义(status=failed)的事件及其关联任务状态。"""
    import asyncio

    from sqlalchemy import select

    from backend.infrastructure.orm import Outbox
    from backend.infrastructure.uow import AsyncUnitOfWork

    async def listing():
        async with AsyncUnitOfWork(settings) as uow:
            events = (
                await uow.session.execute(
                    select(Outbox)
                    .where(Outbox.status == "failed")
                    .order_by(Outbox.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
            result = []
            for event in events:
                job = await uow.jobs.get(uuid.UUID(event.aggregate_id))
                result.append(
                    {
                        "eventId": str(event.id),
                        "eventType": event.event_type,
                        "jobId": event.aggregate_id,
                        "jobStatus": job.status if job else None,
                        "attempts": event.attempts,
                        "createdAt": event.created_at.isoformat()
                        if event.created_at
                        else None,
                    }
                )
            return result

    return asyncio.run(listing())


def requeue(
    settings: DatabaseSettings,
    *,
    event_id,
    operator: str,
    reason: str,
    audit_path: str | Path = DEFAULT_AUDIT_PATH,
) -> dict:
    """重放一条死信事件;任务终态时拒绝;审计记录动作与结果(§九.10)。"""
    import asyncio
    import uuid as uuid_mod


    from backend.infrastructure.uow import AsyncUnitOfWork

    event_id = uuid_mod.UUID(str(event_id))

    async def requeue_one() -> dict:
        from backend.infrastructure.orm import AuditEvent, Job, Outbox
        async with AsyncUnitOfWork(settings) as uow:
            event = await uow.session.get(Outbox, event_id, with_for_update=True)
            if event is None or event.status != "failed":
                return {"requeued": False, "reason": "not-dead"}
            job = await uow.session.get(Job, uuid_mod.UUID(event.aggregate_id), with_for_update=True)
            if job is None:
                return {"requeued": False, "reason": "job-missing"}
            def audit(row):
                if job.provider_snapshot:
                    uow.session.add(AuditEvent(workspace_id=job.workspace_id, actor=operator,
                        action=row["action"], target=str(event_id), details={**row, "reason": reason}))
                else:
                    _audit(audit_path, operator, reason, row)
            if job is not None and job.status in {"completed", "failed", "unknown"}:
                row = {
                    "action": "requeue-denied",
                    "eventId": str(event_id),
                    "jobId": event.aggregate_id,
                    "jobStatus": job.status,
                }
                audit(row)
                return {"requeued": False, "reason": "job-terminal"}
            event.status = "pending"
            event.attempts = 0
            event.next_attempt_at = datetime.now(timezone.utc)
            event.claim_token = None
            row = {
                "action": "requeue",
                "eventId": str(event_id),
                "jobId": event.aggregate_id,
                "eventType": event.event_type,
            }
            audit(row)
            await uow.commit()
            return {"requeued": True, "jobId": event.aggregate_id}

    return asyncio.run(requeue_one())


def _audit(audit_path, operator: str, reason: str, row: dict) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "operator": operator,
        "reason": reason,
        **row,
    }
    path = Path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
