"""Explicit operational actions; no application-level development auth bypass."""
import argparse
import asyncio
import uuid
from datetime import timedelta

from backend.infrastructure.database import create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import AuditEvent, Job, Workspace, WorkspaceMember
from backend.infrastructure.uow import AsyncUnitOfWork
from backend.services.execution import ExecutionStore, utcnow
from backend.services.identity import principal_id


async def grant_member(factory, workspace, issuer, subject, role, actor):
    async with factory.begin() as session:
        row = await session.get(Workspace, workspace, with_for_update=True)
        if row is None:
            row = Workspace(id=workspace)
            session.add(row)
            await session.flush()
        principal = principal_id(issuer, subject)
        member = await session.get(WorkspaceMember, (workspace, principal))
        if member is None:
            session.add(WorkspaceMember(workspace_id=workspace, principal_id=principal, role=role))
        else:
            member.role = role
        session.add(AuditEvent(workspace_id=workspace, actor=actor, action="grant_member",
            target=principal, details={"role": role}))


async def confirm_stopped(factory, job_id, actor, evidence):
    if len(evidence.strip()) < 10:
        raise ValueError("Provide verifiable upstream termination evidence (at least 10 characters)")
    async with AsyncUnitOfWork(factory) as uow:
        session = uow.session
        row = await session.get(Job, job_id, with_for_update=True)
        if row is None or row.status != "unknown" or row.lease_owner:
            raise ValueError("Only an unleased unknown task can be settled by audit")
        row.status, row.phase, row.finished_at = "failed", "audited_stopped", utcnow()
        row.slot_scope = row.slot_acquired_at = row.next_poll_at = None
        row.execution_epoch += 1
        row.error_code, row.recovery = "UPSTREAM_STOP_CONFIRMED", "abandon"
        session.add(AuditEvent(workspace_id=row.workspace_id, actor=actor,
            action="confirm_upstream_stopped", target=str(job_id), details={"evidence": evidence}))
        store = ExecutionStore(factory)
        await store._attempt(uow, row, "failed")
        await store._event(uow, row)


async def resume_reconciliation(factory, job_id, actor, evidence):
    if len(evidence.strip()) < 10:
        raise ValueError("Provide verification of the original endpoint, model and upstream handle")
    async with AsyncUnitOfWork(factory) as uow:
        row = await uow.session.get(Job, job_id, with_for_update=True)
        if row is None or row.status != "unknown" or row.lease_owner or not row.external_task_id or row.kind == "batch":
            raise ValueError("Reconciliation requires an unleased unknown child with its original upstream handle")
        row.phase, row.next_poll_at = "reconcile", utcnow()
        previous = row.reconcile_deadline
        row.reconcile_deadline = utcnow() + timedelta(hours=24)
        row.execution_epoch += 1
        uow.session.add(AuditEvent(workspace_id=row.workspace_id, actor=actor,
            action="resume_reconciliation", target=str(job_id), details={"evidence": evidence,
                "previousDeadline": previous.isoformat() if previous else None,
                "deadline": row.reconcile_deadline.isoformat()}))
        await ExecutionStore(factory)._event(uow, row)


async def unpause_credential(factory, redis, workspace, endpoint, reference, actor, evidence):
    from backend.infrastructure.resilience import scope_key
    if len(evidence.strip()) < 10:
        raise ValueError("Provide evidence that the credential was repaired and verified")
    scope = scope_key(endpoint, reference, "", "credential")
    # Audit the intent before touching Redis. Retrying is safe and creates another
    # audit record; a Redis failure never silently loses the operator's intent.
    async with factory.begin() as session:
        if await session.get(Workspace, workspace) is None:
            raise ValueError("Workspace does not exist")
        session.add(AuditEvent(workspace_id=workspace, actor=actor, action="unpause_credential",
            target=scope, details={"credentialRef": reference, "evidence": evidence}))
    await redis.delete("aivideo:credential:paused:" + scope)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    grant = sub.add_parser("grant-member")
    grant.add_argument("--workspace", required=True, type=uuid.UUID)
    grant.add_argument("--issuer", required=True)
    grant.add_argument("--subject", required=True)
    grant.add_argument("--role", choices=["member", "admin"], default="member")
    grant.add_argument("--actor", required=True)
    stop = sub.add_parser("confirm-stopped")
    stop.add_argument("--job", type=uuid.UUID, required=True)
    stop.add_argument("--evidence", required=True)
    stop.add_argument("--actor", required=True)
    reconcile = sub.add_parser("resume-reconciliation")
    reconcile.add_argument("--job", type=uuid.UUID, required=True)
    reconcile.add_argument("--evidence", required=True)
    reconcile.add_argument("--actor", required=True)
    unpause = sub.add_parser("unpause-credential")
    unpause.add_argument("--workspace", type=uuid.UUID, required=True)
    unpause.add_argument("--endpoint", required=True)
    unpause.add_argument("--reference", required=True)
    unpause.add_argument("--evidence", required=True)
    unpause.add_argument("--actor", required=True)
    args = parser.parse_args()
    async def run():
        import os
        if not os.environ.get("AIVERO_DB_URL"):
            raise RuntimeError("Explicit AIVERO_DB_URL is required")
        engine = create_async_database_engine()
        try:
            factory = create_async_session_factory(engine)
            if args.command == "grant-member":
                await grant_member(factory, args.workspace, args.issuer, args.subject, args.role, args.actor)
            elif args.command == "confirm-stopped":
                await confirm_stopped(factory, args.job, args.actor, args.evidence)
            elif args.command == "resume-reconciliation":
                await resume_reconciliation(factory, args.job, args.actor, args.evidence)
            else:
                from redis.asyncio import Redis
                redis = Redis.from_url(os.environ["AIVERO_REDIS_URL"], socket_timeout=2, socket_connect_timeout=2)
                try:
                    await unpause_credential(factory, redis, args.workspace, args.endpoint, args.reference, args.actor, args.evidence)
                finally:
                    await redis.aclose()
        finally:
            await engine.dispose()
    asyncio.run(run())


if __name__ == "__main__":
    main()
