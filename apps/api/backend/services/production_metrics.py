"""Cross-process operational gauges from PostgreSQL, with bounded label sets."""
from sqlalchemy import func, select

from backend.infrastructure.orm import Job, Outbox
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
    return "\n".join(lines) + "\n"
