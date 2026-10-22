"""Database-serialized processing with explicit contention and integrity outcomes."""
import asyncio
from datetime import timedelta
import logging
import uuid

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from backend.core.event_context import current_event_id
from backend.core.orm import CoreEvent, CoreRun
from .orm import EventAlert, EventAttempt, EventDeadLetter, EventReceipt, RunConsumption
from .registry import Context
from .store import HandlerTransaction, alert, db_now, latest_task
from .integrity import completed, quarantine, quarantined
from .locking import LockBusy, bounded, locked, lock_error

logger = logging.getLogger(__name__)


class IntegrityFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Processor:
    def __init__(self, factory, settings, registry):
        self.factory, self.settings, self.registry = factory, settings, registry

    async def _failure(self, tx, receipt, stream, attempt, error, *, abandoned=False):
        stamp = await db_now(tx)
        attempt.status = "abandoned" if abandoned else "failed"
        attempt.finished_at, attempt.error = stamp, error
        receipt.claim_token = receipt.lease_until = None
        if receipt.attempts >= self.settings.max_attempts:
            receipt.status = "dead"
            stream.blocked_event_id = receipt.event_id
            await tx.execute(insert(EventDeadLetter).values(event_id=receipt.event_id,
                error=error, handler_version=receipt.handler_version, entered_at=stamp)
                .on_conflict_do_update(index_elements=[EventDeadLetter.event_id], set_={
                    "error": error, "handler_version": receipt.handler_version,
                    "entered_at": stamp, "resolved_at": None}))
            await alert(tx, "dead_letter", receipt.event_id, f"dead:{receipt.event_id}:{receipt.generation}")
        else:
            receipt.status = "retry"
            receipt.ready_at = stamp + timedelta(seconds=self.settings.delay(receipt.attempts))
            stream.ready_at = receipt.ready_at

    async def claim(self):
        async with self.factory.begin() as tx:
            await bounded(tx, self.settings)
            stamp = await db_now(tx)
            # Preserve the candidate identity from this snapshot. The cursor may advance
            # before LockRows acquires the Run; never replace it with an assumed next row.
            candidate = (await tx.execute(select(CoreRun, EventReceipt.event_id, EventReceipt.run_sequence)
                .join(RunConsumption, RunConsumption.run_id == CoreRun.id)
                .join(EventReceipt, and_(EventReceipt.run_id == CoreRun.id,
                    EventReceipt.run_sequence == RunConsumption.completed_sequence + 1))
                .where(RunConsumption.blocked_event_id.is_(None), RunConsumption.ready_at <= stamp,
                    ~quarantined(EventReceipt.event_id),
                    or_(and_(EventReceipt.status.in_(["pending", "retry", "waiting_handler"]),
                             EventReceipt.ready_at <= stamp),
                        and_(EventReceipt.status == "running", EventReceipt.lease_until <= stamp)))
                .order_by(RunConsumption.ready_at, CoreRun.id).limit(1)
                .with_for_update(of=CoreRun, skip_locked=True))).first()
            if candidate is None:
                return None
            run, event_id, sequence = candidate
            stream = await locked(tx, RunConsumption, run.id)
            event = await tx.get(CoreEvent, event_id)
            if stream is None or event is None:
                await quarantine(tx, event_id=event_id, run_id=run.id,
                    code="missing_cursor" if stream is None else "missing_event", phase="claim")
                return None
            if await completed(tx, event, stream):
                return None
            receipt = await locked(tx, EventReceipt, event_id)
            if receipt is None:
                await quarantine(tx, event_id=event_id, run_id=run.id, code="missing_receipt", phase="claim")
                return None
            stamp = await db_now(tx)
            if stream.blocked_event_id is not None or stream.ready_at > stamp:
                return None
            if sequence != stream.completed_sequence + 1:
                await quarantine(tx, event_id=event_id, run_id=run.id, code="cursor_mismatch", phase="claim")
                return None
            if receipt.status == "running":
                if receipt.lease_until is None:
                    await quarantine(tx, event_id=event_id, code="missing_lease", phase="claim")
                elif receipt.lease_until <= stamp:
                    attempt = await tx.scalar(select(EventAttempt).where(EventAttempt.claim_token == receipt.claim_token)
                        .with_for_update(skip_locked=True))
                    if attempt is None:
                        if await tx.scalar(select(EventAttempt.id).where(EventAttempt.claim_token == receipt.claim_token)):
                            raise LockBusy("attempt busy")
                        await quarantine(tx, event_id=event_id, code="missing_attempt", phase="claim")
                    else:
                        await self._failure(tx, receipt, stream, attempt, "processing lease expired", abandoned=True)
                return None
            if receipt.status not in {"pending", "retry", "waiting_handler"} or receipt.ready_at > stamp:
                return None
            handler = self.registry.resolve(event)
            if handler is None or (receipt.handler_version and receipt.handler_version != handler.version):
                receipt.status = "waiting_handler"
                receipt.ready_at = stamp + timedelta(seconds=self.settings.missing_handler_poll_seconds)
                stream.ready_at = receipt.ready_at
                await alert(tx, "missing_handler", event.id, f"handler:{event.id}:{receipt.generation}")
                return None
            receipt.status, receipt.handler_version = "running", handler.version
            receipt.attempts += 1
            receipt.claim_token = uuid.uuid4()
            receipt.lease_until = stamp + timedelta(seconds=self.settings.processing_lease_seconds)
            number = await tx.scalar(select(func.coalesce(func.max(EventAttempt.number), 0)).where(
                EventAttempt.event_id == event_id, EventAttempt.generation == receipt.generation))
            tx.add(EventAttempt(event_id=event_id, generation=receipt.generation, number=number + 1,
                claim_token=receipt.claim_token, handler_version=handler.version, status="running"))
            return event_id, run.id, receipt.claim_token

    async def _load(self, tx, claim):
        event_id, run_id, token = claim
        run = await locked(tx, CoreRun, run_id)
        event = await tx.get(CoreEvent, event_id)
        if run is None:
            raise IntegrityFailure("missing_run")
        if event is None:
            raise IntegrityFailure("missing_event")
        if event.run_id != run_id:
            raise IntegrityFailure("run_mismatch")
        stream = await locked(tx, RunConsumption, run_id)
        if stream is None:
            raise IntegrityFailure("missing_cursor")
        if await completed(tx, event, stream):
            return True
        if await tx.scalar(select(quarantined(event_id))):
            return None
        receipt = await locked(tx, EventReceipt, event_id)
        if receipt is None:
            raise IntegrityFailure("missing_receipt")
        if receipt.status != "running" or receipt.claim_token != token:
            logger.debug("core_event stale_or_busy event_id=%s run_id=%s causation_id=%s",
                event_id, run_id, event.causation_id)
            return None
        if receipt.run_sequence != stream.completed_sequence + 1:
            raise IntegrityFailure("cursor_mismatch")
        attempt = await tx.scalar(select(EventAttempt).where(EventAttempt.claim_token == token)
            .with_for_update(skip_locked=True))
        if attempt is None:
            if await tx.scalar(select(EventAttempt.id).where(EventAttempt.claim_token == token)):
                raise LockBusy("attempt busy")
            raise IntegrityFailure("missing_attempt")
        task = await latest_task(tx, event)
        if event.task_id is not None and task is None:
            raise IntegrityFailure("missing_task")
        return run, event, stream, receipt, attempt, task

    async def defer(self, claim):
        # Receipt-only transaction; never acquire a Run after this lock. Retry the
        # metadata CAS outside transactions, without occupying an aggregate lock.
        event_id, run_id, token = claim
        for _ in range(self.settings.lock_recovery_attempts):
            try:
                async with self.factory.begin() as tx:
                    await bounded(tx, self.settings)
                    receipt = await locked(tx, EventReceipt, event_id)
                    if receipt is None:
                        event = await tx.get(CoreEvent, event_id)
                        if event is not None and await completed(tx, event):
                            return
                        await quarantine(tx, event_id=event_id, run_id=run_id,
                            code="missing_receipt" if event else "missing_event", phase="defer")
                        return
                    if receipt.status != "running" or receipt.claim_token != token:
                        logger.debug("core_event stale defer event_id=%s run_id=%s", event_id, run_id)
                        return
                    attempt = await tx.scalar(select(EventAttempt).where(EventAttempt.claim_token == token)
                        .with_for_update(skip_locked=True))
                    if attempt is None:
                        if await tx.scalar(select(EventAttempt.id).where(EventAttempt.claim_token == token)):
                            raise LockBusy("attempt metadata busy")
                        await quarantine(tx, event_id=event_id, run_id=run_id,
                            code="missing_attempt", phase="defer")
                        return
                    attempt.status, attempt.finished_at = "deferred", await db_now(tx)
                    receipt.status, receipt.ready_at = "pending", await db_now(tx) + timedelta(
                        seconds=self.settings.lock_recovery_delay_seconds)
                    receipt.attempts -= 1
                    receipt.claim_token = receipt.lease_until = None
                return
            except Exception as exc:
                if not isinstance(exc, LockBusy) and not lock_error(exc):
                    raise
            await asyncio.sleep(self.settings.lock_recovery_delay_seconds)

    async def execute(self, claim):
        event_id, run_id, token = claim
        try:
            async with self.factory.begin() as tx:
                await bounded(tx, self.settings)
                await tx.execute(text("SELECT set_config('statement_timeout', :value, true)"),
                    {"value": str(self.settings.statement_timeout_ms)})
                loaded = await self._load(tx, claim)
                if loaded is True:
                    return True  # proven completed delivery
                if loaded is None:
                    return False  # stale token or terminal quarantine
                run, event, stream, receipt, attempt, task = loaded
                handler = self.registry.resolve(event)
                if handler is None or handler.version != receipt.handler_version:
                    raise RuntimeError("handler changed after claim")
                ctx_token = current_event_id.set(event.id)
                try:
                    async with asyncio.timeout(self.settings.handler_timeout_seconds):
                        await handler.callback(HandlerTransaction(tx), Context(event, run, task))
                        await tx.flush()
                finally:
                    current_event_id.reset(ctx_token)
                stamp = await db_now(tx)
                receipt.status, receipt.processed_at = "processed", stamp
                receipt.claim_token = receipt.lease_until = None
                stream.completed_sequence, stream.ready_at = receipt.run_sequence, stamp
                attempt.status, attempt.finished_at = "succeeded", stamp
                await tx.execute(update(EventAlert).where(EventAlert.event_id == event.id,
                    EventAlert.kind == "missing_handler", EventAlert.resolved_at.is_(None)).values(resolved_at=stamp))
            return True
        except Exception as exc:
            if isinstance(exc, LockBusy) or lock_error(exc):
                await self.defer(claim)
                return False
            if isinstance(exc, IntegrityFailure):
                async with self.factory.begin() as tx:
                    await bounded(tx, self.settings)
                    return await quarantine(tx, event_id=event_id, run_id=run_id, code=exc.code, phase="execute")
            from backend.observability import redact_text
            error = redact_text(f"{type(exc).__name__}: {exc}")[:self.settings.error_max_chars]
            try:
                async with self.factory.begin() as tx:
                    await bounded(tx, self.settings)
                    loaded = await self._load(tx, claim)
                    if loaded is None or loaded is True:
                        return False
                    _, _, stream, receipt, attempt, _ = loaded
                    await self._failure(tx, receipt, stream, attempt, error)
            except IntegrityFailure as missing:
                async with self.factory.begin() as tx:
                    await bounded(tx, self.settings)
                    await quarantine(tx, event_id=event_id, run_id=run_id, code=missing.code, phase="failure")
            except Exception as failure:
                if not isinstance(failure, LockBusy) and not lock_error(failure):
                    raise
                await self.defer(claim)
            return False

    async def once(self):
        try:
            claim = await self.claim()
            return await self.execute(claim) if claim else False
        except Exception as exc:
            if isinstance(exc, LockBusy) or lock_error(exc):
                return False
            raise

    async def tick(self):
        return await asyncio.gather(*(self.once() for _ in range(self.settings.concurrency)))
