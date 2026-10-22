"""Recovery watchdog; stale publication/processing claims are reclaimed by their owners."""
from datetime import timedelta
from sqlalchemy import or_, select, update
from backend.core.orm import CoreEvent
from .orm import EventAlert, EventReceipt, EventAttempt, RunConsumption
from .locking import bounded
from .integrity import completed, completion_proof, quarantine, quarantined
from backend.core.orm import CoreRun
from .store import alert, db_now


async def watchdog(factory, settings):
    async with factory.begin() as tx:
        await bounded(tx, settings)
        # Find records that cannot reach the ordinary Run-joined claim query.
        broken = (await tx.execute(select(EventReceipt.event_id, EventReceipt.run_id,
            CoreEvent.id, CoreRun.id, RunConsumption.run_id).select_from(EventReceipt)
            .outerjoin(CoreEvent, CoreEvent.id == EventReceipt.event_id)
            .outerjoin(CoreRun, CoreRun.id == EventReceipt.run_id)
            .outerjoin(RunConsumption, RunConsumption.run_id == EventReceipt.run_id)
            .where(~quarantined(EventReceipt.event_id), or_(CoreEvent.id.is_(None),
                CoreRun.id.is_(None), RunConsumption.run_id.is_(None))).limit(settings.batch_size))).all()
        for ident, run_id, envelope, run, cursor in broken:
            code = "missing_event" if envelope is None else "missing_run" if run is None else "missing_cursor"
            await quarantine(tx, event_id=ident, run_id=run_id, code=code, phase="recovery")
        missing_receipts = (await tx.scalars(select(EventAttempt.event_id).outerjoin(
            EventReceipt, EventReceipt.event_id == EventAttempt.event_id)
            .where(EventReceipt.event_id.is_(None), EventAttempt.status == "running",
                ~quarantined(EventAttempt.event_id)).distinct().limit(settings.batch_size))).all()
        for ident in missing_receipts:
            event = await tx.get(CoreEvent, ident)
            if event is not None and await completed(tx, event):
                continue
            await quarantine(tx, event_id=ident, code="missing_receipt", phase="recovery")
        stamp = await db_now(tx)
        # Completed receipts may be cleaned. A retained success/cursor is stronger
        # evidence than a missing inbox row; finish any crashed publication mark.
        settled = select(CoreEvent.id).where(CoreEvent.delivery_status != "published",
            completion_proof(CoreEvent.id, CoreEvent.run_id, CoreEvent.run_sequence))            .limit(settings.batch_size).with_for_update(skip_locked=True)
        await tx.execute(update(CoreEvent).where(CoreEvent.id.in_(settled))
            .values(delivery_status="published", published_at=stamp, claim_token=None, lease_until=None,
                next_attempt_at=None))
        oldest = await tx.scalar(select(CoreEvent.id).outerjoin(EventReceipt, EventReceipt.event_id == CoreEvent.id)
            .where(CoreEvent.created_at < stamp - timedelta(seconds=settings.backlog_alert_seconds),
                or_(EventReceipt.event_id.is_(None), EventReceipt.status != "processed"),
                ~quarantined(CoreEvent.id),
                ~completion_proof(CoreEvent.id, CoreEvent.run_id, CoreEvent.run_sequence))
            .order_by(CoreEvent.created_at).limit(1))
        if oldest:
            await alert(tx, "backlog", oldest, "core-event-backlog")
        else:
            await tx.execute(update(EventAlert).where(EventAlert.dedupe_key == "core-event-backlog",
                EventAlert.resolved_at.is_(None)).values(resolved_at=stamp))
