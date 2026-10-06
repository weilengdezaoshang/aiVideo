"""Durable execution ownership and deadlines; network I/O belongs outside these transactions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update

from backend.errors import ErrorCode, Recovery
from backend.infrastructure.orm import AssetReference, Attempt, Job
from backend.infrastructure.uow import AsyncUnitOfWork
from backend.providers.policy import Certainty, Operation, ProviderFailure, ProviderPolicy


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ExecutionClaim:
    job_id: uuid.UUID
    owner: str
    epoch: int
    phase: str
    deadline: datetime
    params: dict
    snapshot: dict
    payload: dict
    external_task_id: str | None
    kind: str


class ExecutionStore:
    def __init__(self, session_factory, policy: ProviderPolicy | None = None):
        self.factory = session_factory
        self.policy = policy or ProviderPolicy()

    async def _locked(self, uow, job_id):
        return (await uow.session.execute(
            select(Job).where(Job.id == job_id).with_for_update()
        )).scalar_one_or_none()

    async def _owned(self, uow, claim):
        row = await self._locked(uow, claim.job_id)
        if (row is None or row.lease_owner != claim.owner
                or row.execution_epoch != claim.epoch or row.lease_until is None
                or row.lease_until <= utcnow()):
            return None
        return row

    async def _event(self, uow, row):
        row.state_version += 1
        row.updated_at = utcnow()
        if row.parent_id:
            # Child rows are locked before their parent everywhere. Read committed
            # aggregation converges after concurrent child commits without retries.
            await uow.session.flush()
            parent = await self._locked(uow, row.parent_id)
            siblings = list(await uow.session.scalars(select(Job).where(Job.parent_id == parent.id)))
            statuses = {child.status for child in siblings}
            parent.status = ("running" if statuses & {"queued", "running"} else
                             "unknown" if "unknown" in statuses else
                             "failed" if "failed" in statuses else "completed")
            parent.progress = sum(child.progress or 0 for child in siblings) / max(1, len(siblings))
            parent.state_version += 1
            parent.updated_at = utcnow()
            if parent.status in {"completed", "failed"}:
                parent.finished_at = utcnow()
        await uow.outbox.enqueue(
            aggregate_type="job", aggregate_id=str(row.id), event_type="job.changed",
            payload={"jobId": str(row.id), "workspaceId": str(row.workspace_id),
                     "stateVersion": row.state_version},
        )

    async def claim(self, job_id: uuid.UUID) -> ExecutionClaim | None:
        now = utcnow()
        async with AsyncUnitOfWork(self.factory) as uow:
            row = await self._locked(uow, job_id)
            if row is None or (row.status not in {"queued", "running"} and not (
                    row.status == "unknown" and row.phase in {"reconcile", "cancel"})):
                return None
            # Only the recovery scanner may fence an expired owner: it checks its
            # artifact receipt before deciding whether submission was uncertain.
            if row.lease_owner is not None:
                return None
            if row.next_poll_at is not None and row.next_poll_at > now:
                return None
            if row.phase == "submitting":
                await self._unknown(uow, row)
                return None
            if row.cancel_requested_at is not None and row.phase not in {"cancel", "reconcile", "download", "persist"}:
                if row.external_task_id or row.slot_scope:
                    await self._unknown(uow, row)
                else:
                    row.status, row.recovery = "failed", Recovery.ABANDON.value
                    row.finished_at = now
                    await self._event(uow, row)
                return None
            if row.execution_deadline is None:
                if row.queue_deadline is not None and row.queue_deadline <= now:
                    row.status, row.error_code = "failed", "QUEUE_TIMEOUT"
                    row.recovery, row.finished_at = Recovery.RETRY.value, now
                    await self._event(uow, row)
                    return None
                seconds = float(row.provider_snapshot.get(
                    "videoTimeoutMin" if row.kind == "video" else "imageTimeoutMin",
                    60 if row.kind == "video" else 20)) * 60
                if row.kind == "export":
                    seconds = self.policy.export_s
                row.execution_deadline = now + timedelta(seconds=seconds)
            deadline = (row.download_deadline or row.execution_deadline) if row.phase in {"download", "persist"} else row.execution_deadline
            if row.phase in {"reconcile", "cancel"}:
                deadline = row.reconcile_deadline
                if deadline is None or deadline <= now or not row.external_task_id:
                    row.phase = "reconcile_exhausted"
                    row.next_poll_at = None
                    await self._event(uow, row)
                    return None
            if deadline <= now:
                if row.phase in {"download", "persist"}:
                    row.status, row.error_code = "failed", "ARTIFACT_DOWNLOAD_TIMEOUT"
                    row.recovery, row.finished_at = Recovery.CONTACT.value, now
                    row.slot_scope = row.slot_acquired_at = None
                    await self._event(uow, row)
                elif row.external_task_id or row.slot_scope:
                    await self._unknown(uow, row)
                else:
                    row.status, row.error_code = "failed", ErrorCode.UPSTREAM_TIMEOUT.value
                    row.recovery, row.finished_at = Recovery.RETRY.value, now
                    await self._event(uow, row)
                return None
            row.lease_owner = uuid.uuid4().hex
            if row.phase == "export":
                row.status = "running"
            row.lease_until = now + timedelta(seconds=self.policy.lease_s)
            row.execution_epoch += 1
            row.next_poll_at = None
            row.dispatched_until = None
            await self._event(uow, row)
            return ExecutionClaim(row.id, row.lease_owner, row.execution_epoch, row.phase,
                                  deadline, dict(row.params), dict(row.provider_snapshot),
                                  dict(row.phase_payload), row.external_task_id, row.kind)

    async def heartbeat(self, claim: ExecutionClaim) -> bool:
        async with AsyncUnitOfWork(self.factory) as uow:
            row = await self._owned(uow, claim)
            if row is None:
                return False
            row.lease_until = utcnow() + timedelta(seconds=self.policy.lease_s)
            return row.cancel_requested_at is None or row.phase in {"cancel", "reconcile", "download", "persist"}

    async def begin_submit(self, claim: ExecutionClaim, scope: str, limit: int) -> bool:
        async with AsyncUnitOfWork(self.factory) as uow:
            row = await self._owned(uow, claim)
            if row is None or row.cancel_requested_at or row.phase != "submit":
                return False
            if not await uow.jobs.try_acquire_slot(row.id, scope=scope, limit=limit):
                row.next_poll_at = utcnow() + timedelta(seconds=2)
                row.lease_owner = row.lease_until = None
                return False
            row.phase, row.status = "submitting", "running"
            row.attempt_count += 1
            uow.session.add(Attempt(job_id=row.id, attempt_no=row.attempt_count,
                                    provider=str(row.provider_snapshot.get("provider", "unknown"))))
            await self._event(uow, row)
            return True

    async def advance(self, claim: ExecutionClaim, phase: str, payload: dict,
                      *, external_id: str | None = None, delay_s: float = 0,
                      upstream_finished: bool = False) -> bool:
        async with AsyncUnitOfWork(self.factory) as uow:
            row = await self._owned(uow, claim)
            if row is None:
                return False
            row.phase, row.phase_payload, row.phase_failures = phase, payload, 0
            if external_id is not None:
                row.external_task_id = external_id
            if claim.phase in {"reconcile", "cancel"} and phase == "poll":
                row.phase, row.status = "reconcile", "unknown"
                delay_s = max(30, delay_s)
            elif phase in {"download", "persist"}:
                row.status = "running"
            if phase == "download" and row.download_deadline is None:
                row.download_deadline = utcnow() + timedelta(seconds=self.policy.download_recovery_s)
            if phase == "failed":
                row.error_code, row.recovery = ErrorCode.UPSTREAM_UNAVAILABLE.value, Recovery.CONTACT.value
            if upstream_finished:
                row.slot_scope = row.slot_acquired_at = None
                await self._attempt(uow, row, "failed" if phase == "failed" else "completed")
            elif external_id:
                await self._attempt(uow, row, "running")
            if phase == "failed":
                row.status, row.finished_at = "failed", utcnow()
                row.next_poll_at = None
            else:
                row.next_poll_at = utcnow() + timedelta(seconds=delay_s)
            row.lease_owner = row.lease_until = None
            await self._event(uow, row)
            return True

    async def complete(self, claim: ExecutionClaim, artifact: dict) -> bool:
        async with AsyncUnitOfWork(self.factory) as uow:
            row = await self._owned(uow, claim)
            if row is None:
                return False
            asset = await uow.assets.add(row.workspace_id, kind="video" if row.kind == "export" else row.kind,
                ext=artifact["ext"], storage_key=artifact["storage_key"],
                size=artifact["bytes"], sha256=artifact["sha256"])
            asset.width = int(row.params.get("width", 0))
            asset.height = int(row.params.get("height", 0))
            asset.details = {"history": row.kind != "export", "jobId": str(row.id),
                "provider": row.provider_snapshot.get("provider", "unknown"),
                "params": {k: v for k, v in row.params.items() if k not in {
                    "referenceStorage", "maskStorage", "submittedBy", "sources"}}}
            uow.session.add(AssetReference(asset_id=asset.id, owner_type="job", owner_id=str(row.id)))
            row.phase_payload = {**artifact, "assetId": str(asset.id)}
            row.phase, row.status = "completed", "completed"
            row.progress, row.finished_at = 1.0, utcnow()
            row.error_code = row.recovery = None
            row.lease_owner = row.lease_until = row.slot_scope = row.slot_acquired_at = None
            row.next_poll_at = None
            await self._attempt(uow, row, "completed")
            await self._event(uow, row)
            return True

    async def _attempt(self, uow, row, status: str):
        if not row.attempt_count:
            return
        await uow.session.execute(update(Attempt).where(
            Attempt.job_id == row.id, Attempt.attempt_no == row.attempt_count
        ).values(status=status, provider_task_id=row.external_task_id,
                 error_code=row.error_code,
                 finished_at=utcnow() if status in {"completed", "failed"} else None))

    async def defer(self, claim: ExecutionClaim, delay_s: float) -> bool:
        async with AsyncUnitOfWork(self.factory) as uow:
            row = await self._owned(uow, claim)
            if row is None:
                return False
            row.next_poll_at = utcnow() + timedelta(seconds=delay_s)
            row.lease_owner = row.lease_until = None
            await self._event(uow, row)
            return True

    async def fail(self, claim: ExecutionClaim, failure: ProviderFailure) -> bool:
        import random

        async with AsyncUnitOfWork(self.factory) as uow:
            row = await self._owned(uow, claim)
            if row is None:
                return False
            row.error_code, row.recovery = failure.code.value, failure.recovery.value
            uncertain = (failure.operation == Operation.SUBMIT
                         and failure.certainty == Certainty.UNKNOWN)
            if uncertain:
                await self._unknown(uow, row)
            elif failure.retryable and row.phase_failures < self.policy.max_retries:
                row.phase_failures += 1
                delay = failure.retry_after if failure.retry_after is not None else random.uniform(1, 2 ** row.phase_failures)
                row.next_poll_at = utcnow() + timedelta(seconds=delay)
                if row.phase == "submitting":
                    await self._attempt(uow, row, "failed")
                    row.phase = "submit"
                    row.slot_scope = row.slot_acquired_at = None
                await self._event(uow, row)
            elif row.external_task_id and row.phase in {"poll", "cancel", "reconcile"}:
                await self._unknown(uow, row)
            else:
                row.status, row.finished_at = "failed", utcnow()
                row.slot_scope = row.slot_acquired_at = None
                await self._attempt(uow, row, "failed")
                await self._event(uow, row)
            row.lease_owner = row.lease_until = None
            return True

    async def _unknown(self, uow, row):
        row.status = "unknown"
        row.error_code, row.recovery = ErrorCode.UPSTREAM_UNKNOWN.value, Recovery.RECONCILE.value
        row.lease_owner = row.lease_until = None
        row.next_poll_at = None
        if row.external_task_id:
            if row.reconcile_deadline is None:
                row.reconcile_deadline = utcnow() + timedelta(seconds=self.policy.reconcile_s)
            if row.reconcile_deadline > utcnow():
                row.phase = "reconcile"
                row.next_poll_at = utcnow() + timedelta(seconds=30)
            else:
                row.phase = "reconcile_exhausted"
        await self._attempt(uow, row, "unknown")
        # Deliberately retain model occupancy: local timeout does not stop the upstream.
        await self._event(uow, row)

    async def request_cancel(self, job_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        async with AsyncUnitOfWork(self.factory) as uow:
            row = await self._locked(uow, job_id)
            if row is None or row.workspace_id != workspace_id:
                return False
            if row.status in {"completed", "failed"}:
                return True
            row.cancel_requested_at = utcnow()
            if row.lease_owner is not None:
                # The active owner observes the flag on heartbeat. Fencing and
                # submission uncertainty are handled by lease recovery.
                await self._event(uow, row)
                return True
            if row.external_task_id:
                await self._unknown(uow, row)
                if row.provider_snapshot.get("provider") == "cloud" and row.kind == "video":
                    row.phase = "cancel"
                row.next_poll_at = utcnow()
            elif row.slot_scope or row.phase == "submitting":
                await self._unknown(uow, row)
            else:
                row.status, row.finished_at = "failed", utcnow()
                row.error_code, row.recovery = "CANCELLED", Recovery.ABANDON.value
                await self._event(uow, row)
            return True

    async def recover_expired(self, limit: int = 100, spool=None) -> int:
        async with AsyncUnitOfWork(self.factory) as uow:
            rows = list((await uow.session.execute(select(Job).where(
                Job.status.in_(["queued", "running", "unknown"]), Job.lease_until < func.now()
            ).order_by(Job.parent_id, Job.id).limit(limit).with_for_update(skip_locked=True))).scalars())
            for row in rows:
                try:
                    artifact = spool.read(row.id, row.execution_epoch) if spool else None
                except (OSError, ValueError, KeyError, TypeError):
                    # A bad receipt must neither poison the entire sweep nor allow
                    # a submitted task to be generated again.
                    artifact = None
                row.execution_epoch += 1
                row.lease_owner = row.lease_until = None
                if artifact is not None:
                    row.phase, row.phase_payload = "persist", artifact
                    if row.download_deadline is None:
                        row.download_deadline = utcnow() + timedelta(seconds=self.policy.download_recovery_s)
                    row.next_poll_at = utcnow()
                    row.slot_scope = row.slot_acquired_at = None
                    await self._event(uow, row)
                elif row.phase == "submitting":
                    await self._unknown(uow, row)
                else:
                    row.next_poll_at = utcnow()
                    await self._event(uow, row)
            return len(rows)

    async def expire_due(self, limit: int = 100) -> int:
        """Deadline enforcement must work even when no broker or Worker can run."""
        now = utcnow()
        async with AsyncUnitOfWork(self.factory) as uow:
            rows = list(await uow.session.scalars(select(Job).where(
                Job.lease_owner.is_(None), Job.kind != "batch",
                or_(
                    (Job.status.in_(["queued", "running"])) & or_(
                        Job.execution_deadline.is_(None) & (Job.queue_deadline <= now),
                        Job.phase.in_(["download", "persist"]) & (Job.download_deadline <= now),
                        ~Job.phase.in_(["download", "persist"]) & (Job.execution_deadline <= now)),
                    (Job.status == "unknown") & Job.phase.in_(["reconcile", "cancel"]) & (Job.reconcile_deadline <= now))
            ).order_by(Job.parent_id, Job.id).limit(limit).with_for_update(skip_locked=True)))
            for row in rows:
                if row.status == "unknown":
                    row.phase, row.next_poll_at = "reconcile_exhausted", None
                    await self._event(uow, row)
                elif row.phase in {"download", "persist"}:
                    row.status, row.finished_at = "failed", now
                    row.error_code, row.recovery = "ARTIFACT_DOWNLOAD_TIMEOUT", "contact"
                    row.next_poll_at = None
                    await self._event(uow, row)
                elif row.external_task_id or row.slot_scope:
                    await self._unknown(uow, row)
                else:
                    row.status, row.finished_at = "failed", now
                    row.error_code = "QUEUE_TIMEOUT" if row.execution_deadline is None else "UPSTREAM_TIMEOUT"
                    row.recovery, row.next_poll_at = "retry", None
                    await self._event(uow, row)
            return len(rows)
