"""Block 2 contention/integrity regression cases for the block 7 collection."""
import asyncio
import logging
import os
import time
import uuid

import pytest
from sqlalchemy import delete, event as sql_event, func, select
from sqlalchemy.exc import DBAPIError

from backend.core.orm import CoreEvent, CoreRun, CoreSession
from backend.core.events.contracts import EventSettings, EventType
from backend.core.events.orm import EventAlert, EventAttempt, EventQuarantine, EventReceipt, EventTimer, RunConsumption
from backend.core.events.processor import Processor
from backend.core.events.registry import Registry
from backend.core.events.store import accept, db_now
from backend.core.events.timers import Timers, schedule
from backend.core.events.recovery import watchdog
from backend.core.events.integrity import replay_quarantine
from test_core_events import fixture, events, receive_all, settings

pytestmark = pytest.mark.skipif(not os.getenv("AIVERO_CORE_EVENTS_TEST_DB"), reason="dedicated core-event database required")


@pytest.mark.parametrize("locked_count,hold_seconds", [(1, 30), (5, 3)])
def test_locked_runs_do_not_starve_other_timers(locked_count, hold_seconds):
    async def scenario():
        from datetime import timedelta
        config = EventSettings(batch_size=2, poll_seconds=.01, timer_lease_seconds=.2,
            timer_backoff_base_seconds=.03, timer_backoff_max_seconds=.2,
            timer_skip_alert_threshold=2, timer_accuracy_seconds=.6)
        async with fixture(locked_count + 2) as (factory, runs):
            ids = []
            async with factory.begin() as tx:
                due = await db_now(tx) + timedelta(seconds=.2)
                for run in runs:
                    root = await tx.scalar(select(CoreEvent).where(CoreEvent.run_id == run))
                    timer = await schedule(tx, run, EventType.TIMEOUT, due,
                        idempotency_key="lock-regression", causation_id=root.id)
                    ids.append(timer.id)
            stop = asyncio.Event()
            service = Timers(factory, config)
            async def daemon():
                while not stop.is_set():
                    await service.tick()
                    await asyncio.sleep(config.poll_seconds)
            task = None
            try:
                async with factory.begin() as held:
                    await held.execute(select(CoreRun).where(CoreRun.id.in_(runs[:locked_count])).with_for_update())
                    started = time.monotonic()
                    task = asyncio.create_task(daemon())
                    await asyncio.sleep(.2 + config.timer_accuracy_seconds)
                    async with factory() as tx:
                        for ident in ids[locked_count:]:
                            row = await tx.get(EventTimer, ident)
                            assert row.status == "fired"
                            assert 0 <= (row.fired_at - due).total_seconds() <= config.timer_accuracy_seconds
                    # Healthy-Run latency and repeated-skip alerts are separate
                    # obligations: inspect alerts after the configured hold window,
                    # not after the first healthy-timer accuracy deadline.
                    await asyncio.sleep(max(0, hold_seconds - (time.monotonic() - started)))
                    async with factory() as tx:
                        for ident in ids[:locked_count]:
                            row = await tx.get(EventTimer, ident)
                            assert row.status == "scheduled" and row.lock_skip_count >= config.timer_skip_alert_threshold
                            assert row.next_fire_at >= row.due_at
                            alert = await tx.scalar(select(EventAlert).where(EventAlert.dedupe_key == f"timer-lock:{ident}"))
                            assert alert is not None and alert.resolved_at is None
                released = time.monotonic()
                while time.monotonic() - released < config.timer_backoff_max_seconds + config.timer_accuracy_seconds:
                    async with factory() as tx:
                        statuses = (await tx.scalars(select(EventTimer.status).where(EventTimer.id.in_(ids)))).all()
                    if all(status == "fired" for status in statuses):
                        break
                    await asyncio.sleep(config.poll_seconds)
                assert all(status == "fired" for status in statuses)
                async with factory() as tx:
                    for ident in ids:
                        assert await tx.scalar(select(func.count()).select_from(CoreEvent).where(
                            CoreEvent.payload["timer_id"].astext == str(ident))) == 1
                    for ident in ids[:locked_count]:
                        alert = await tx.scalar(select(EventAlert).where(EventAlert.dedupe_key == f"timer-lock:{ident}"))
                        assert alert.resolved_at is not None
            finally:
                stop.set()
                if task:
                    await task
    asyncio.run(scenario())


def test_last_event_candidate_snapshot_advances_without_next_receipt(caplog):
    async def scenario():
        async with fixture() as (factory, runs):
            root = (await events(factory, runs[0], 0))[0]
            await receive_all(factory, [root])
            processor = Processor(factory, settings(), Registry())
            claim = await processor.claim()
            async with factory.begin() as tx:
                receipt = await tx.get(EventReceipt, root.id)
                from datetime import timedelta
                receipt.lease_until = await db_now(tx) - timedelta(seconds=1)
            engine = factory.kw["bind"]
            gate, armed = asyncio.Event(), True
            @sql_event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
            def delay(conn, cursor, statement, parameters, context, executemany):
                nonlocal armed
                if armed and "JOIN core_run_consumption" in statement and "FOR UPDATE" in statement:
                    armed = False
                    gate.set()
                    statement = statement.replace(" ORDER BY", " AND pg_sleep(0.3) IS NOT NULL ORDER BY")
                return statement, parameters
            async def competing():
                await gate.wait()
                await asyncio.sleep(.05)
                assert await processor.execute(claim)
            try:
                result, _ = await asyncio.gather(processor.claim(), competing())
                assert result is None
            finally:
                sql_event.remove(engine.sync_engine, "before_cursor_execute", delay)
            async with factory() as tx:
                assert (await tx.get(EventReceipt, root.id)).status == "processed"
                assert await tx.scalar(select(func.count()).select_from(EventQuarantine).where(EventQuarantine.event_id == root.id)) == 0
    with caplog.at_level(logging.DEBUG, logger="backend.core.events.integrity"):
        asyncio.run(scenario())
    assert "already_processed" in caplog.text


def test_two_consumers_cleanup_processed_receipt_and_duplicate_ack(caplog):
    async def scenario():
        async with fixture() as (factory, runs):
            rows = await events(factory, runs[0], 1)
            await receive_all(factory, rows)
            registry = Registry()
            async def handler(tx, ctx):
                from sqlalchemy import update
                await tx.execute(update(CoreSession).where(CoreSession.id == ctx.run.session_id)
                    .values(language=CoreSession.language + "!"))
            registry.register(EventType.USER_MESSAGE, "v1", handler)
            processor = Processor(factory, settings(), registry)
            await processor.once()
            claim = await processor.claim()
            await asyncio.gather(processor.execute(claim), processor.execute(claim))
            # If a contender deferred before either executor started, resume the single intent.
            for _ in range(20):
                async with factory() as tx:
                    if (await tx.get(EventReceipt, rows[1].id)).status == "processed":
                        break
                await asyncio.sleep(.02)
                await processor.once()
            async with factory.begin() as tx:
                assert (await tx.get(EventReceipt, rows[1].id)).status == "processed"
                run = await tx.get(CoreRun, runs[0])
                assert (await tx.get(CoreSession, run.session_id)).language == "zh!"
                await tx.execute(delete(EventReceipt).where(EventReceipt.event_id == rows[1].id))
            async with factory.begin() as tx:
                assert await accept(tx, {"event_id": str(rows[1].id), "run_id": str(runs[0])}) == rows[1].id
            assert await processor.execute(claim)
            await watchdog(factory, settings())
            async with factory() as tx:
                assert await tx.get(EventReceipt, rows[1].id) is None
                assert await tx.scalar(select(func.count()).select_from(EventAttempt).where(
                    EventAttempt.event_id == rows[1].id, EventAttempt.status == "succeeded")) == 1
                assert await tx.scalar(select(func.count()).select_from(EventQuarantine).where(EventQuarantine.event_id == rows[1].id)) == 0
    with caplog.at_level(logging.DEBUG, logger="backend.core.events.integrity"):
        asyncio.run(scenario())
    assert "already_processed" in caplog.text


@pytest.mark.parametrize("missing", ["run", "receipt", "cursor", "attempt", "event"])
def test_missing_dependencies_are_terminal_and_alerted(missing, monkeypatch):
    async def scenario():
        async with fixture() as (factory, runs):
            root = (await events(factory, runs[0], 0))[0]
            await receive_all(factory, [root])
            processor = Processor(factory, settings(), Registry())
            claim = await processor.claim()
            if missing in {"receipt", "cursor", "attempt"}:
                model = {"receipt": EventReceipt, "cursor": RunConsumption, "attempt": EventAttempt}[missing]
                column = model.run_id if missing == "cursor" else model.event_id
                value = runs[0] if missing == "cursor" else root.id
                async with factory.begin() as tx:
                    await tx.execute(delete(model).where(column == value))
            elif missing == "run":
                # Production FK/row locks forbid this deletion; inject the impossible
                # read result to exercise defense against externally corrupted storage.
                with pytest.raises(DBAPIError):
                    async with factory.begin() as tx:
                        await tx.execute(delete(CoreRun).where(CoreRun.id == runs[0]))
                import backend.core.events.processor as module
                original = module.locked
                async def absent_run(tx, model, ident):
                    if model is CoreRun and ident == runs[0]:
                        return None
                    return await original(tx, model, ident)
                monkeypatch.setattr(module, "locked", absent_run)
            else:
                from sqlalchemy.ext.asyncio import AsyncSession
                original = AsyncSession.get
                async def absent_event(self, model, ident, **kwargs):
                    if model is CoreEvent and ident == root.id:
                        return None
                    return await original(self, model, ident, **kwargs)
                monkeypatch.setattr(AsyncSession, "get", absent_event)
            assert not await processor.execute(claim)
            for _ in range(3):
                assert not await processor.once()
            async with factory() as tx:
                record = await tx.scalar(select(EventQuarantine).where(EventQuarantine.event_id == root.id))
                assert record.code == f"missing_{missing}"
                assert record.context["event_id"] == str(root.id) and record.context["run_id"] == str(runs[0])
                assert "causation_id" in record.context
                alert = await tx.scalar(select(EventAlert).where(EventAlert.dedupe_key == f"quarantine:event:{root.id}"))
                assert alert is not None and alert.resolved_at is None
    asyncio.run(scenario())


def test_unknown_notification_is_durably_quarantined_without_fk():
    async def scenario():
        async with fixture() as (factory, runs):
            ident = uuid.uuid4()
            async with factory.begin() as tx:
                await accept(tx, {"event_id": str(ident), "run_id": str(runs[0])})
            async with factory.begin() as tx:
                await accept(tx, {"event_id": str(ident), "run_id": str(runs[0])})
            async with factory() as tx:
                records = (await tx.scalars(select(EventQuarantine).where(EventQuarantine.event_id == ident))).all()
                assert len(records) == 1 and records[0].code == "missing_event"
    asyncio.run(scenario())


def test_missing_receipt_quarantine_can_be_repaired_and_audited():
    async def scenario():
        async with fixture() as (factory, runs):
            root = (await events(factory, runs[0], 0))[0]
            await receive_all(factory, [root])
            processor = Processor(factory, settings(), Registry())
            claim = await processor.claim()
            async with factory.begin() as tx:
                await tx.execute(delete(EventReceipt).where(EventReceipt.event_id == root.id))
            assert not await processor.execute(claim)
            key = uuid.uuid4().hex
            async with factory.begin() as tx:
                record = await tx.scalar(select(EventQuarantine).where(EventQuarantine.event_id == root.id))
                audit = await replay_quarantine(tx, record, operator="review", reason="restore receipt", request_key=key)
                assert audit.generation == 1
            assert await processor.once()
            async with factory() as tx:
                assert (await tx.get(EventReceipt, root.id)).status == "processed"
    asyncio.run(scenario())


def test_timer_claim_crash_recovery_and_stale_token_without_early_firing():
    async def scenario():
        from datetime import timedelta
        from sqlalchemy import update
        async with fixture() as (factory, runs):
            root = (await events(factory, runs[0], 0))[0]
            config = EventSettings(timer_lease_seconds=.05)
            service = Timers(factory, config)
            async with factory.begin() as tx:
                timer = await schedule(tx, runs[0], EventType.TIMEOUT, await db_now(tx) + timedelta(seconds=.1),
                    idempotency_key="crash", causation_id=root.id)
            assert await service.claim() is None
            await asyncio.sleep(.12)
            old = await service.claim()
            assert old
            # Claim committed, process vanished before firing; a restart sees its lease.
            await asyncio.sleep(.07)
            restarted = Timers(factory, config)
            assert await restarted.tick() == 1
            assert not await service.fire(old)
            async with factory() as tx:
                row = await tx.get(EventTimer, timer.id)
                assert row.status == "fired" and row.fired_at >= row.due_at
                assert await tx.scalar(select(func.count()).select_from(CoreEvent).where(
                    CoreEvent.payload["timer_id"].astext == str(timer.id))) == 1
            # Standalone timer row contention is also excluded before LIMIT.
            async with factory.begin() as tx:
                timer2 = await schedule(tx, runs[0], EventType.TIMEOUT, await db_now(tx),
                    idempotency_key="row-lock", causation_id=root.id)
            async with factory.begin() as held:
                await held.execute(select(EventTimer).where(EventTimer.id == timer2.id).with_for_update())
                assert await service.claim() is None
            assert await restarted.tick() == 1
            async with factory.begin() as tx:
                # Retry metadata does not invalidate a user's cancellation version.
                await tx.execute(update(EventTimer).where(EventTimer.id == timer2.id).values(lock_skip_count=1))
    asyncio.run(scenario())


def test_publisher_and_receiver_do_not_wait_on_locked_outbox():
    async def scenario():
        from backend.core.events.transport import Publisher, Receiver
        from backend.core.events.locking import lock_error
        async with fixture(2) as (factory, runs):
            roots = [(await events(factory, run, 0))[0] for run in runs]
            class Transport:
                settings = EventSettings()
                def send(self, body):
                    assert body.run_id == runs[1]
            transport = Transport()
            async with factory.begin() as held:
                await held.execute(select(CoreEvent).where(CoreEvent.id == roots[0].id).with_for_update())
                started = time.monotonic()
                assert await Publisher(factory, settings(), transport).once()
                assert time.monotonic() - started < transport.settings.timer_accuracy_seconds
                # FK checks also take locks: a receiver must requeue, rather than
                # hold its whole broker batch behind this unrelated transaction.
                with pytest.raises(DBAPIError) as failure:
                    await Receiver(factory, transport).persist({"event_id": str(roots[0].id), "run_id": str(runs[0])})
                assert lock_error(failure.value)
    asyncio.run(scenario())


def test_missing_receipt_watchdog_without_running_processor():
    async def scenario():
        async with fixture() as (factory, runs):
            root = (await events(factory, runs[0], 0))[0]
            await receive_all(factory, [root])
            processor = Processor(factory, settings(), Registry())
            await processor.claim()
            async with factory.begin() as tx:
                await tx.execute(delete(EventReceipt).where(EventReceipt.event_id == root.id))
            await watchdog(factory, settings())
            async with factory() as tx:
                record = await tx.scalar(select(EventQuarantine).where(EventQuarantine.event_id == root.id))
                assert record.code == "missing_receipt" and record.context["phase"] == "recovery"
    asyncio.run(scenario())


def test_timer_integrity_failure_has_audited_schedule_repair(monkeypatch):
    async def scenario():
        from backend.core.events.integrity import replay_timer_quarantine
        import backend.core.events.timers as module
        async with fixture() as (factory, runs):
            root = (await events(factory, runs[0], 0))[0]
            async with factory.begin() as tx:
                timer = await schedule(tx, runs[0], EventType.TIMEOUT, await db_now(tx),
                    idempotency_key="quarantine-repair", causation_id=root.id)
            service = Timers(factory, settings())
            claim = await service.claim()
            original = module.locked
            async def missing_run(tx, model, ident):
                return None if model is CoreRun else await original(tx, model, ident)
            monkeypatch.setattr(module, "locked", missing_run)
            assert not await service.fire(claim)
            monkeypatch.setattr(module, "locked", original)
            key = uuid.uuid4().hex
            async with factory.begin() as tx:
                record = await tx.scalar(select(EventQuarantine).where(EventQuarantine.timer_id == timer.id))
                assert (await tx.get(EventTimer, timer.id)).status == "quarantined"
                audit = await replay_timer_quarantine(tx, record, operator="test", reason="restore Run", request_key=key)
                again = await replay_timer_quarantine(tx, record, operator="test", reason="restore Run", request_key=key)
                assert audit == again and len(record.context["replays"]) == 1
            assert await service.tick() == 1
    asyncio.run(scenario())


def test_cleaned_success_receipts_do_not_starve_recovery_or_republish():
    async def scenario():
        from backend.core.events.transport import Publisher
        async with fixture(2) as (factory, runs):
            roots = [(await events(factory, run, 0))[0] for run in runs]
            processor = Processor(factory, settings(), Registry())
            await receive_all(factory, roots[:1])
            assert await processor.once()
            await receive_all(factory, roots[1:])
            await processor.claim()
            async with factory.begin() as tx:
                await tx.execute(delete(EventReceipt).where(EventReceipt.event_id.in_([row.id for row in roots])))
            # A single-row batch must not keep selecting the benign cleaned success.
            await watchdog(factory, EventSettings(batch_size=1))
            async with factory() as tx:
                record = await tx.scalar(select(EventQuarantine).where(EventQuarantine.event_id == roots[1].id))
                assert record.code == "missing_receipt"
                assert (await tx.get(CoreEvent, roots[0].id)).delivery_status == "published"
            class Transport:
                def send(self, body):
                    raise AssertionError("completed or quarantined events must never republish")
            assert await Publisher(factory, settings(), Transport()).claim() is None
    asyncio.run(scenario())


def test_bad_notification_dead_letter_does_not_poison_genuine_event():
    async def scenario():
        from backend.core.events.transport import Receiver
        from backend.core.events.integrity import quarantined
        async with fixture(2) as (factory, runs):
            root = (await events(factory, runs[0], 0))[0]
            class Transport:
                settings = EventSettings()
            receiver = Receiver(factory, Transport())
            await receiver.persist({"event_id": str(root.id), "run_id": str(runs[1])})
            await receiver.persist({"event_id": str(root.id), "run_id": str(runs[0]), "unexpected": "excluded"})
            async with factory() as tx:
                records = (await tx.scalars(select(EventQuarantine).where(EventQuarantine.event_id == root.id))).all()
                assert {record.code for record in records} == {"run_mismatch", "invalid_claim"}
                assert all("causation_id" in record.context for record in records)
                assert not await tx.scalar(select(quarantined(root.id)))
            await receiver.persist({"event_id": str(root.id), "run_id": str(runs[0])})
            assert await Processor(factory, settings(), Registry()).once()
    asyncio.run(scenario())


def test_delete_during_live_handler_is_prevented_without_harming_event():
    async def scenario():
        from backend.core.events.locking import bounded, lock_error
        async with fixture() as (factory, runs):
            rows = await events(factory, runs[0], 1)
            await receive_all(factory, rows)
            entered, release = asyncio.Event(), asyncio.Event()
            registry = Registry()
            async def handler(tx, ctx):
                entered.set()
                await release.wait()
            registry.register(EventType.USER_MESSAGE, "v1", handler)
            processor = Processor(factory, settings(), registry)
            await processor.once()
            claim = await processor.claim()
            active = asyncio.create_task(processor.execute(claim))
            try:
                await entered.wait()
                with pytest.raises(DBAPIError) as failure:
                    async with factory.begin() as tx:
                        await bounded(tx, settings())
                        await tx.execute(delete(CoreRun).where(CoreRun.id == runs[0]))
                assert lock_error(failure.value)
            finally:
                release.set()
                assert await active
            async with factory() as tx:
                assert (await tx.get(EventReceipt, rows[1].id)).status == "processed"
                assert await tx.scalar(select(func.count()).select_from(EventQuarantine).where(EventQuarantine.event_id == rows[1].id)) == 0
    asyncio.run(scenario())
