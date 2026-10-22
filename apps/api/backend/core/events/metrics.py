"""Bounded labels: never use Run/event IDs as Prometheus labels."""
from sqlalchemy import func, select
from backend.core.orm import CoreEvent
from .contracts import EventSettings
from .orm import EventAlert, EventReceipt, EventTimer
from .store import db_now


async def render_event_metrics(factory):
    lines = []
    async with factory() as tx:
        for model, column, name, states in (
            (CoreEvent, CoreEvent.delivery_status, "outbox", ("pending", "claimed", "published", "failed")),
            (EventReceipt, EventReceipt.status, "receipts", ("pending", "running", "retry", "waiting_handler", "processed", "dead")),
            (EventTimer, EventTimer.status, "timers", ("scheduled", "fired", "cancelled"))):
            counts = dict((await tx.execute(select(column, func.count()).select_from(model).group_by(column))).all())
            lines.append(f"# TYPE aivideo_core_{name} gauge")
            lines.extend(f'aivideo_core_{name}{{status="{state}"}} {counts.get(state, 0)}' for state in states)
        counts = dict((await tx.execute(select(EventAlert.kind, func.count())
            .where(EventAlert.resolved_at.is_(None)).group_by(EventAlert.kind))).all())
        for kind in ("dead_letter", "missing_handler", "publish_failure", "invalid_claim", "backlog"):
            lines.append(f'aivideo_core_alerts{{kind="{kind}"}} {counts.get(kind, 0)}')
        oldest = await tx.scalar(select(func.min(CoreEvent.created_at)).where(CoreEvent.delivery_status != "published"))
        stamp = await db_now(tx)
        lines.append(f"aivideo_core_outbox_oldest_seconds {max(0, (stamp - oldest).total_seconds()) if oldest else 0}")
        lines.append(f"aivideo_core_backlog_threshold_seconds {EventSettings().backlog_alert_seconds}")
    return lines
