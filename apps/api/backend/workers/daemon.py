"""Broker-independent dispatcher/scheduler and confirmed event bridge.

Run one role per process: python -m backend.workers.daemon ROLE.
No repository config or paid-provider credentials are loaded here.
"""

import argparse
import logging
import os
from pathlib import Path
import signal
import socket
import threading
import uuid

from kombu import Connection
from sqlalchemy import func, or_, select

from backend.infrastructure.artifacts import ArtifactSpool
from backend.infrastructure.database import DatabaseSettings
from backend.infrastructure.messaging import (
    AGENT_PHASE_QUEUE, NOTIFICATION_QUEUE, PHASE_QUEUE, declare_topology,
)
from backend.infrastructure.orm import Job
from backend.infrastructure.outbox_dispatcher import OutboxDispatcher
from backend.infrastructure.uow import AsyncUnitOfWork
from backend.services.execution import ExecutionStore, utcnow
from backend.workers.celery_app import celery_app
from backend.workers.durable import phase_budget
from backend.workers.runtime import get_runtime

logger = logging.getLogger(__name__)
WORK_QUEUE = PHASE_QUEUE


async def schedule_once(factory, root: Path, limit: int = 100) -> int:
    from backend.core.execution.engine import scan_ready
    await scan_ready(factory)
    from backend.core.sessions.dispatch import dispatch_due
    from backend.core.supervisor.wiring import deployment_routes, preferred_route
    await dispatch_due(factory, agent_route=preferred_route(deployment_routes(), None))
    from backend.core.session_projection import project_session_once
    await project_session_once(factory, limit=limit)
    from backend.core.version_switch import VersionSwitchWorker
    await VersionSwitchWorker(factory).run_once()
    from backend.core.recognition.worker import RecognitionWorker
    await RecognitionWorker(factory, root).run_once()
    from backend.core.supervisor.subjects import expire_stale_choices
    await expire_stale_choices(factory)
    from backend.core.execution.projection import project_pending
    await project_pending(factory, limit)
    from backend.agent.runtime import AgentService
    await AgentService(factory).recover_due(limit=limit)
    await ExecutionStore(factory).recover_expired(spool=ArtifactSpool(root))
    await ExecutionStore(factory).expire_due()
    async with AsyncUnitOfWork(factory) as uow:
        eligible = select(Job.id, func.row_number().over(
            partition_by=Job.workspace_id, order_by=(Job.created_at, Job.id)
        ).label("position")).where(
            or_(Job.status.in_(["queued", "running"]),
                (Job.status == "unknown") & Job.phase.in_(["reconcile", "cancel"])),
            Job.provider_snapshot != {},
            Job.kind != "batch",
            Job.lease_owner.is_(None),
            or_(Job.next_poll_at.is_(None), Job.next_poll_at <= func.now()),
            or_(Job.dispatched_until.is_(None), Job.dispatched_until <= func.now())
        ).subquery()
        rows = list((await uow.session.execute(select(Job).join(eligible, eligible.c.id == Job.id)
            .order_by(eligible.c.position, Job.created_at, Job.id)
            .limit(limit).with_for_update(skip_locked=True, of=Job))).scalars())
        from datetime import timedelta
        for row in rows:
            # A visibility deadline repairs a lost wakeup; atomic claim absorbs duplicates.
            row.dispatched_until = utcnow() + timedelta(seconds=30)
            await uow.outbox.enqueue(aggregate_type="job", aggregate_id=str(row.id),
                event_type="execution.due", payload={"jobId": str(row.id)})
        return len(rows)


def bridge_once(connection, factory_runtime, limit: int = 50) -> int:
    processed = 0

    def callback(body, message):
        nonlocal processed
        processed += 1
        if not isinstance(body, dict) or body.get("schemaVersion") != "1" or body.get("eventType") != "execution.due":
            message.reject(requeue=False)
            return
        try:
            ident = uuid.UUID(body["payload"]["jobId"])
        except (KeyError, ValueError, TypeError):
            message.reject(requeue=False)
            return

        async def metadata():
            factory = factory_runtime.session_factory()
            async with factory() as session:
                job = await session.get(Job, ident)
                if job is None or (job.status not in {"queued", "running"} and not (
                        job.status == "unknown" and job.phase in {"reconcile", "cancel"})):
                    return None
                return job.kind, await phase_budget(factory, ident)

        try:
            data = factory_runtime.run(metadata(), timeout=10)
            if data is not None:
                kind, budget = data
                if kind not in {"image", "video", "export"}:
                    message.reject(requeue=False)
                    return
                # Reuse this confirmed channel, avoid unconfirmed publish-then-ACK.
                celery_app.send_task("aivideo.execute_phase", args=[str(ident)],
                    queue="exports" if kind == "export" else f"generation.{kind}", producer=connection.Producer(),
                    retry=False, delivery_mode=2, soft_time_limit=budget + 30,
                    time_limit=budget + 60)
            message.ack()
        except Exception:
            reject_failed(message)
            raise

    with connection.Consumer([WORK_QUEUE], callbacks=[callback], accept=["json"], prefetch_count=1):
        while processed < limit:
            try:
                connection.drain_events(timeout=1)
            except socket.timeout:
                break
    return processed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("dispatcher", "scheduler", "bridge", "agent-bridge", "notify"))
    role = parser.parse_args().role
    from backend.observability import setup_logging
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"), "json")
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    settings = DatabaseSettings()
    runtime = get_runtime(settings)
    broker = os.environ["AIVERO_BROKER_URL"]
    try:
        while not stopped.is_set():
            try:
                if role == "dispatcher":
                    dispatcher = OutboxDispatcher(settings, broker)
                    dispatcher.reclaim_stale_publishing()
                    dispatcher.dispatch_once()
                elif role == "scheduler":
                    async def tick():
                        return await schedule_once(runtime.session_factory(), Path(os.environ["SWARMUI_DATA_DIR"]))
                    runtime.run(tick(), timeout=30)
                else:
                    with Connection(broker, transport_options={"confirm_publish": True}, connect_timeout=5) as connection:
                        declare_topology(connection)
                        if role == "notify":
                            notify_once(connection, runtime)
                        elif role == "agent-bridge":
                            agent_bridge_once(connection, runtime)
                        else:
                            bridge_once(connection, runtime)
            except Exception:
                logger.exception("%s iteration failed; retrying after backoff", role)
            stopped.wait(2)
    finally:
        runtime.close()


def notify_once(connection, runtime):
    def callback(body, message):
        if not isinstance(body, dict) or body.get("schemaVersion") != "1" or body.get("eventType") not in {"job.changed", "core.changed"}:
            message.reject(requeue=False)
            return
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        import json
        async def publish():
            async with runtime.session_factory()() as session:
                if body["eventType"] == "core.changed":
                    from backend.core.orm import CoreRun, CoreSession
                    if payload.get("runId"):
                        run = await session.get(CoreRun, uuid.UUID(str(payload["runId"])))
                        if run is not None:
                            await runtime.redis().publish("aivideo:notify:" + str(run.workspace_id), json.dumps({
                                "runId": str(run.id), "workspaceId": str(run.workspace_id)}))
                        return
                    if payload.get("sessionId"):
                        core_session = await session.get(CoreSession, uuid.UUID(str(payload["sessionId"])))
                        if core_session is not None:
                            await runtime.redis().publish(
                                "aivideo:notify:" + str(core_session.workspace_id), json.dumps({
                                    "sessionId": str(core_session.id),
                                    "workspaceId": str(core_session.workspace_id),
                                    "userId": core_session.user_id}))
                        return
                    if payload.get("commandId"):
                        from backend.core.version_switch_orm import VersionSwitchCommand
                        command = await session.get(VersionSwitchCommand, uuid.UUID(str(payload["commandId"])))
                        if command is not None:
                            await runtime.redis().publish("aivideo:notify:" + str(command.workspace_id), json.dumps({
                                "commandId": str(command.id), "documentId": str(command.document_id)}))
                        return
                    raise KeyError("core.changed payload requires runId, sessionId or commandId")
                ident = uuid.UUID(payload["jobId"])
                job = await session.get(Job, ident)
                if job is not None:
                    # Read the latest version: duplicate/old notifications cannot regress state.
                    await runtime.redis().publish("aivideo:notify:" + str(job.workspace_id), json.dumps({
                        "jobId": str(job.id), "workspaceId": str(job.workspace_id),
                        "stateVersion": job.state_version}))
        try:
            runtime.run(publish(), timeout=10)
            message.ack()
        except (KeyError, ValueError, TypeError):
            message.reject(requeue=False)
            return
        except Exception:
            reject_failed(message)
            raise
    with connection.Consumer([NOTIFICATION_QUEUE], callbacks=[callback], accept=["json"], prefetch_count=1):
        try:
            connection.drain_events(timeout=1)
        except socket.timeout:
            pass


def reject_failed(message):
    """Quorum delivery counters bound explicit requeues; DLQ retains the envelope.

    Lost channels are recovered by RabbitMQ. The scheduler can independently repair
    lost phase wakeups, and SSE snapshots repair notifications without DLQ replay.
    """
    deliveries = (message.headers or {}).get("x-delivery-count", 0)
    message.reject(requeue=isinstance(deliveries, int) and deliveries < 3)


def agent_bridge_once(connection, runtime, limit: int = 50) -> int:
    """Confirmed handoff of Agent wakeups; Runtime absorbs duplicate delivery."""
    processed = 0

    def callback(body, message):
        nonlocal processed
        processed += 1
        if not isinstance(body, dict) or body.get("schemaVersion") != "1" or body.get("eventType") != "agent.due":
            message.reject(requeue=False)
            return
        try:
            ident = uuid.UUID(body["payload"]["runId"])
        except (KeyError, ValueError, TypeError):
            message.reject(requeue=False)
            return
        try:
            celery_app.send_task("aivideo.agent.advance", args=[str(ident)], queue="agent",
                producer=connection.Producer(), retry=False, delivery_mode=2,
                soft_time_limit=0, time_limit=300)
            message.ack()
        except Exception:
            reject_failed(message)
            raise

    with connection.Consumer([AGENT_PHASE_QUEUE], callbacks=[callback], accept=["json"], prefetch_count=1):
        while processed < limit:
            try:
                connection.drain_events(timeout=1)
            except socket.timeout:
                break
    return processed


if __name__ == "__main__":
    main()
