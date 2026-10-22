"""Cross-process operational gauges from PostgreSQL, with bounded label sets."""
from sqlalchemy import func, select

from backend.infrastructure.orm import Job, Outbox
from backend.agent.orm import AgentRun, HostCreditLedger, HostNodeLease
from backend.services.execution import utcnow


async def render_metrics(factory):
    lines = ["# TYPE aivideo_jobs gauge", "# TYPE aivideo_outbox gauge"]
    async with factory() as session:
        jobs = await session.execute(select(Job.status, func.count()).where(Job.kind != "batch").group_by(Job.status))
        counts = dict(jobs.all())
        for state in ("queued", "running", "unknown", "completed", "failed"):
            lines.append(f'aivideo_jobs{{status="{state}"}} {counts.get(state, 0)}')
        outbox = await session.execute(select(Outbox.status, func.count()).group_by(Outbox.status))
        counts = dict(outbox.all())
        for state in ("pending", "publishing", "published", "failed"):
            lines.append(f'aivideo_outbox{{status="{state}"}} {counts.get(state, 0)}')
        oldest = await session.scalar(select(func.min(Outbox.created_at)).where(Outbox.status.in_(["pending", "publishing"])))
        age = max(0, (utcnow() - oldest).total_seconds()) if oldest else 0
        lines.append(f"aivideo_outbox_oldest_seconds {age}")
        leased = await session.scalar(select(func.count()).select_from(Job).where(Job.lease_until < func.now()))
        lines.append(f"aivideo_expired_leases {leased}")
        occupied = await session.scalar(select(func.count()).select_from(Job).where(Job.slot_scope.is_not(None)))
        lines.append(f"aivideo_upstream_slots {occupied}")
        timeouts = await session.scalar(select(func.count()).select_from(Job).where(Job.error_code.like("%TIMEOUT%")))
        lines.append(f"aivideo_timeout_jobs {timeouts}")
        retries = await session.scalar(select(func.coalesce(func.sum(Job.phase_failures), 0)))
        lines.append(f"aivideo_phase_failures {retries}")
        lines.extend(["# TYPE aivideo_host_runs gauge", "# TYPE aivideo_host_reservations gauge"])
        rows = await session.execute(select(AgentRun.status, func.count()).where(AgentRun.phase == "media").group_by(AgentRun.status))
        states = dict(rows.all())
        for state in ("queued", "running", "awaiting_confirm", "awaiting_user_final", "canceling", "cancelled", "completed", "failed", "blocked", "needs_attention"):
            lines.append(f'aivideo_host_runs{{status="{state}"}} {states.get(state, 0)}')
        rows = await session.execute(select(HostCreditLedger.state, func.count()).group_by(HostCreditLedger.state))
        states = dict(rows.all())
        for state in ("reserved", "settled", "refunded", "cancelled_with_charge"):
            lines.append(f'aivideo_host_reservations{{status="{state}"}} {states.get(state, 0)}')
        oldest = await session.scalar(select(func.min(HostCreditLedger.created_at)).where(HostCreditLedger.state == "reserved"))
        lines.append(f"aivideo_host_reservation_oldest_seconds {max(0, (utcnow() - oldest).total_seconds()) if oldest else 0}")
        expired = await session.scalar(select(func.count()).select_from(HostNodeLease).where(HostNodeLease.expires_at < func.now()))
        lines.append(f"aivideo_host_expired_node_leases {expired}")
        cancelled = await session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.phase == "media", AgentRun.error["code"].astext == "cancel_timeout"))
        lines.append(f"aivideo_host_cancel_timeouts {cancelled}")
    from backend.core.events.metrics import render_event_metrics
    lines.extend(await render_event_metrics(factory))
    return "\n".join(lines) + "\n"
