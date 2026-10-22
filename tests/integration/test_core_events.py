"""Opt-in, isolated real PostgreSQL and RabbitMQ reliability tests."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import os
import json
import signal
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import func, select, text, update

from backend.core.contracts import RunInput
from backend.core.orm import CoreEvent, CoreRun, CoreSession
from backend.core.store import create_run
from backend.core.events.contracts import EventSettings, EventType
from backend.core.events.orm import EventAttempt, EventDeadLetter, EventReceipt, EventReplay, EventTimer, RunConsumption
from backend.core.events.processor import Processor
from backend.core.events.registry import Registry
from backend.core.events.store import accept, db_now, emit, replay
from backend.core.events.timers import Timers, cancel, schedule
from backend.core.events.transport import Publisher, RabbitTransport, Receiver
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Workspace

DB = os.getenv("AIVERO_CORE_EVENTS_TEST_DB")
BROKER = os.getenv("AIVERO_CORE_EVENTS_TEST_BROKER")
pytestmark = pytest.mark.skipif(not DB, reason="explicit dedicated migrated core-event PostgreSQL required")


def settings(**kwargs):
    return EventSettings(partitions=2, batch_size=200, concurrency=10, backoff_base_seconds=.01,
        backoff_max_seconds=.02, backoff_jitter=0, poll_seconds=.1, **kwargs)


@asynccontextmanager
async def fixture(count=1):
    engine = create_async_database_engine(DatabaseSettings(url=DB, pool_size=12, max_overflow=10))
    factory = create_async_session_factory(engine)
    async with factory.begin() as tx:
        workspace = Workspace(id=uuid.uuid4())
        tx.add(workspace)
        await tx.flush()
        session = CoreSession(workspace_id=workspace.id, source_canvas_id=uuid.uuid4(), user_id="events-test", language="zh")
        tx.add(session)
        await tx.flush()
        runs = []
        for _ in range(count):
            run = await create_run(tx, RunInput(session_id=session.id, workspace_id=workspace.id,
                user_id="events-test", request_key=uuid.uuid4().hex, request_hash="a"*64,
                snapshot={}, scope={}, invariants={}, version_lock={}))
            runs.append(run.id)
    try:
        yield factory, runs
    finally:
        # Retain no event heads that could interfere with other tests' global processor.
        async with factory.begin() as tx:
            await tx.execute(update(RunConsumption).where(RunConsumption.run_id.in_(runs))
                .values(ready_at=func.now() + text("interval '100 years'")))
            await tx.execute(update(CoreEvent).where(CoreEvent.run_id.in_(runs)).values(
                delivery_status="published", published_at=func.now() + text("interval '100 years'")))
            await tx.execute(update(EventTimer).where(EventTimer.run_id.in_(runs), EventTimer.status == "scheduled")
                .values(status="cancelled"))
        await engine.dispose()


async def events(factory, run_id, count):
    async with factory.begin() as tx:
        root = await tx.scalar(select(CoreEvent).where(CoreEvent.run_id == run_id).order_by(CoreEvent.run_sequence))
        rows = [root]
        for _ in range(count):
            rows.append(await emit(tx, EventType.USER_MESSAGE, run_id, causation_id=root.id))
        return rows


async def receive_all(factory, rows):
    async def one(row):
        async with factory.begin() as tx:
            await accept(tx, {"event_id": str(row.id), "run_id": str(row.run_id)})
    await asyncio.gather(*(one(row) for row in rows))


async def drain(processor, factory, runs):
    for _ in range(250):
        await processor.tick()
        async with factory() as tx:
            pending = await tx.scalar(select(func.count()).select_from(EventReceipt).where(
                EventReceipt.run_id.in_(runs), EventReceipt.status != "processed"))
        if not pending:
            return
        await asyncio.sleep(.01)
    raise AssertionError("processor did not drain")


def test_100_events_duplicates_reverse_delivery_and_10_runs_parallel():
    async def scenario():
        async with fixture(10) as (factory, runs):
            rows = []
            for run in runs:
                rows.extend(await events(factory, run, 100 if run == runs[0] else 1))
            # Complete technical creation events before synchronizing ten domain handlers.
            roots = [row for row in rows if row.run_sequence == 1]
            await receive_all(factory, roots)
            await drain(Processor(factory, settings(), Registry()), factory, runs)
            await receive_all(factory, list(reversed(rows)) * 2)
            active, peak, sequence = set(), 0, {run: [] for run in runs}
            all_started = asyncio.Event()
            registry = Registry()
            async def handler(tx, ctx):
                nonlocal peak
                assert ctx.run.id not in active
                active.add(ctx.run.id)
                peak = max(peak, len(active))
                try:
                    if ctx.event.run_sequence == 2:
                        if len(active) == 10:
                            all_started.set()
                        await asyncio.wait_for(all_started.wait(), 5)
                    await asyncio.sleep(.02)
                    sequence[ctx.run.id].append(ctx.event.run_sequence)
                finally:
                    active.remove(ctx.run.id)
            registry.register(EventType.USER_MESSAGE, "v1", handler)
            p = Processor(factory, settings(), registry)
            # Keep independent consumers polling while other handlers await the
            # overlap barrier. A batch-wide gather could strand deferred Runs.
            stop = asyncio.Event()
            async def consumer():
                while not stop.is_set():
                    await p.once()
                    await asyncio.sleep(.001)
            async def all_processed():
                while True:
                    async with factory() as tx:
                        pending = await tx.scalar(select(func.count()).select_from(EventReceipt).where(
                            EventReceipt.run_id.in_(runs), EventReceipt.status != "processed"))
                    if pending == 0:
                        return
                    await asyncio.sleep(.01)
            consumers = [asyncio.create_task(consumer()) for _ in range(2 * p.settings.concurrency)]
            try:
                await asyncio.wait_for(all_processed(), 30)
            finally:
                stop.set()
                await asyncio.gather(*consumers)
            assert sequence[runs[0]] == list(range(2, 102))
            assert peak == 10
            async with factory() as tx:
                assert await tx.scalar(select(func.count()).select_from(EventReceipt).where(
                    EventReceipt.run_id.in_(runs))) == len(rows)
                assert await tx.scalar(select(func.count()).select_from(EventAttempt).join(
                    EventReceipt, EventReceipt.event_id == EventAttempt.event_id).where(
                    EventReceipt.run_id.in_(runs), EventAttempt.status != "deferred")) == len(rows)
    asyncio.run(scenario())


def test_failure_rolls_back_effects_dead_letter_and_audited_replay():
    async def scenario():
        async with fixture() as (factory, runs):
            rows = await events(factory, runs[0], 2)
            await receive_all(factory, rows)
            fail = True
            registry = Registry()
            async def handler(tx, ctx):
                await tx.execute(update(CoreSession).where(CoreSession.id == ctx.run.session_id).values(language="effect"))
                await tx.emit(EventType.TIMEOUT, ctx.run.id)
                if fail:
                    raise RuntimeError("test failure")
            registry.register(EventType.USER_MESSAGE, "v1", handler)
            p = Processor(factory, settings(max_attempts=2), registry)
            for _ in range(8):
                await p.tick()
                await asyncio.sleep(.03)
            async with factory() as tx:
                run = await tx.get(CoreRun, runs[0])
                assert (await tx.get(CoreSession, run.session_id)).language == "zh"
                assert await tx.scalar(select(func.count()).select_from(CoreEvent).where(
                    CoreEvent.run_id == runs[0], CoreEvent.event_type == "timeout")) == 0
                assert (await tx.get(EventReceipt, rows[1].id)).status == "dead"
                assert (await tx.get(EventReceipt, rows[2].id)).status == "pending"
                assert (await tx.get(EventDeadLetter, rows[1].id)).error.endswith("test failure")
            result = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "backend.workers.core_events",
                "inspect", str(rows[1].id)], env={**os.environ, "PYTHONPATH": "apps/api", "AIVERO_DB_URL": DB},
                capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            inspected = json.loads(result.stdout)
            assert inspected["status"] == "dead" and len(inspected["causal_chain"]["events"]) == 2
            key = uuid.uuid4().hex
            async def do_replay():
                async with factory.begin() as tx:
                    return (await replay(tx, rows[1].id, operator="test", reason="fixed", request_key=key)).generation
            assert await asyncio.gather(do_replay(), do_replay()) == [1, 1]
            fail = False
            await drain(p, factory, runs)
            async with factory() as tx:
                assert await tx.scalar(select(func.count()).select_from(EventReplay).where(EventReplay.event_id == rows[1].id)) == 1
            with pytest.raises(ValueError):
                async with factory.begin() as tx:
                    await replay(tx, rows[1].id, operator="test", reason="bad", request_key=uuid.uuid4().hex)
    asyncio.run(scenario())


def test_missing_handler_gap_and_recovery_of_abandoned_claim():
    async def scenario():
        async with fixture() as (factory, runs):
            rows = await events(factory, runs[0], 1)
            # Receipt for sequence 2 cannot bypass missing sequence 1.
            await receive_all(factory, rows[1:])
            p = Processor(factory, settings(processing_lease_seconds=.3, missing_handler_poll_seconds=.01), Registry())
            assert await p.claim() is None
            await receive_all(factory, rows[:1])
            await p.once()
            assert await p.claim() is None
            async with factory() as tx:
                receipt = await tx.get(EventReceipt, rows[1].id)
                assert receipt.status == "waiting_handler" and receipt.attempts == 0
            async def handler(tx, ctx):
                pass
            p.registry.register(EventType.USER_MESSAGE, "v1", handler)
            await asyncio.sleep(.02)
            claim = await p.claim()
            assert claim
            await asyncio.sleep(.35)
            assert await p.claim() is None  # marks crashed attempt abandoned
            assert not await p.execute(claim)  # stale token fenced
            await asyncio.sleep(.02)
            await drain(p, factory, runs)
            async with factory() as tx:
                attempts = (await tx.scalars(select(EventAttempt).where(EventAttempt.event_id == rows[1].id)
                    .order_by(EventAttempt.number))).all()
                assert [a.status for a in attempts if a.status != "deferred"] == ["abandoned", "succeeded"]
                assert (await tx.get(EventReceipt, rows[1].id)).attempts == 2
    asyncio.run(scenario())


def test_timers_atomic_cancel_cas_and_restart(tmp_path):
    async def scenario():
        async with fixture() as (factory, runs):
            root = (await events(factory, runs[0], 0))[0]
            async with factory.begin() as tx:
                due = await db_now(tx) + timedelta(seconds=.1)
                timer = await schedule(tx, runs[0], EventType.TIMEOUT, due, idempotency_key="ttl", causation_id=root.id)
                assert (await schedule(tx, runs[0], EventType.TIMEOUT, due, idempotency_key="ttl", causation_id=root.id)).id == timer.id
                other = await schedule(tx, runs[0], EventType.TIMEOUT, due, idempotency_key="cancel", causation_id=root.id)
                await cancel(tx, other.id, 0)
            # A completely new process/engine reads the persisted schedule.
            await asyncio.sleep(.15)
            code = '''import asyncio, os
from backend.infrastructure.database import *
from backend.core.events.timers import Timers
from backend.core.events.contracts import EventSettings
async def main():
 e=create_async_database_engine(DatabaseSettings(url=os.environ["AIVERO_CORE_TEST_DB"]))
 await Timers(create_async_session_factory(e),EventSettings()).tick()
 await e.dispose()
asyncio.run(main())'''
            result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code],
                env={**os.environ, "PYTHONPATH": "apps/api", "AIVERO_CORE_TEST_DB": DB}, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            assert await Timers(factory, settings()).tick() == 0
            async with factory() as tx:
                timer = await tx.get(EventTimer, timer.id)
                event = await tx.get(CoreEvent, timer.event_id)
                assert timer.status == "fired" and timer.version == 1
                assert event.causation_id == root.id and event.payload["timer_id"] == str(timer.id)
                assert (await tx.get(EventTimer, other.id)).status == "cancelled"
    asyncio.run(scenario())


@pytest.mark.skipif(not BROKER, reason="explicit isolated RabbitMQ required")
def test_sigkill_after_real_broker_send_before_mark_and_duplicate_effect(tmp_path):
    async def scenario():
        async with fixture() as (factory, runs):
            rows = await events(factory, runs[0], 1)
            root = rows[1]
            await receive_all(factory, rows[:1])
            await Processor(factory, settings(), Registry()).once()
            async with factory.begin() as tx:
                await tx.execute(update(CoreEvent).where(CoreEvent.id == rows[0].id).values(
                    delivery_status="published", published_at=func.now() + text("interval '100 years'")))
            marker = tmp_path / "sent"
            code = '''import asyncio, os
from pathlib import Path
from backend.infrastructure.database import *
from backend.core.events.contracts import EventSettings
from backend.core.events.transport import Publisher, RabbitTransport
async def main():
 e=create_async_database_engine(DatabaseSettings(url=os.environ["AIVERO_CORE_TEST_DB"]))
 s=EventSettings(partitions=2,publish_lease_seconds=.1)
 async def after(body):
  Path(os.environ["SENT_MARKER"]).write_text(str(body.event_id))
  await asyncio.Event().wait()
 await Publisher(create_async_session_factory(e),s,RabbitTransport(os.environ["AIVERO_CORE_TEST_BROKER"],s),after).once()
asyncio.run(main())'''
            proc = subprocess.Popen([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": "apps/api",
                "AIVERO_CORE_TEST_DB": DB, "AIVERO_CORE_TEST_BROKER": BROKER, "SENT_MARKER": str(marker)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for _ in range(200):
                    if marker.exists() or proc.poll() is not None:
                        break
                    await asyncio.sleep(.05)
                assert marker.exists(), (await asyncio.to_thread(proc.communicate, timeout=2))[1].decode()
                assert marker.read_text() == str(root.id)
                os.kill(proc.pid, signal.SIGKILL)
                await asyncio.to_thread(proc.wait, timeout=5)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            s = settings(publish_lease_seconds=.1)
            transport = RabbitTransport(BROKER, s)
            await asyncio.sleep(.15)
            assert await Publisher(factory, s, transport).once()
            receiver = Receiver(factory, transport)
            assert await receiver.tick() >= 2
            registry = Registry()
            async def handler(tx, ctx):
                await tx.execute(update(CoreSession).where(CoreSession.id == ctx.run.session_id)
                    .values(language=CoreSession.language + "!"))
            registry.register(EventType.USER_MESSAGE, "v1", handler)
            await drain(Processor(factory, s, registry), factory, runs)
            async with factory() as tx:
                run = await tx.get(CoreRun, runs[0])
                assert (await tx.get(CoreSession, run.session_id)).language == "zh!"
                assert (await tx.get(CoreEvent, root.id)).attempts == 2
                assert (await tx.get(EventReceipt, root.id)).status == "processed"
                assert await tx.scalar(select(func.count()).select_from(EventAttempt).where(EventAttempt.event_id == root.id)) == 1
    asyncio.run(scenario())


def test_expired_lease_never_overlaps_active_transaction_and_derived_event_is_atomic():
    async def scenario():
        async with fixture() as (factory, runs):
            rows = await events(factory, runs[0], 1)
            await receive_all(factory, rows)
            entered, release = asyncio.Event(), asyncio.Event()
            registry = Registry()
            async def handler(tx, ctx):
                await tx.emit(EventType.TIMEOUT, ctx.run.id)
                entered.set()
                await release.wait()
            registry.register(EventType.USER_MESSAGE, "v1", handler)
            p = Processor(factory, settings(processing_lease_seconds=.01), registry)
            await p.once()
            claim = await p.claim()
            executing = asyncio.create_task(p.execute(claim))
            await entered.wait()
            await asyncio.sleep(.03)
            assert await p.claim() is None  # SKIP LOCKED even though lease expired
            async with factory() as tx:
                assert await tx.scalar(select(func.count()).select_from(CoreEvent).where(
                    CoreEvent.run_id == runs[0], CoreEvent.event_type == "timeout")) == 0
                assert (await tx.get(EventReceipt, rows[1].id)).status == "running"
            release.set()
            assert await executing
            async with factory() as tx:
                result = await tx.scalar(select(CoreEvent).where(CoreEvent.run_id == runs[0], CoreEvent.event_type == "timeout"))
                assert result.causation_id == rows[1].id
                assert (await tx.get(EventReceipt, rows[1].id)).status == "processed"
    asyncio.run(scenario())


def test_emission_payload_causal_trace_and_timer_rollback():
    async def scenario():
        from backend.core.events.store import causal_chain
        from backend.core.states import VersionConflict
        from sqlalchemy.exc import DBAPIError
        async with fixture() as (factory, runs):
            root = (await events(factory, runs[0], 0))[0]
            with pytest.raises(ValueError):
                async with factory.begin() as tx:
                    await emit(tx, EventType.TIMEOUT, runs[0])
            with pytest.raises(ValueError):
                async with factory.begin() as tx:
                    await emit(tx, EventType.TIMEOUT, runs[0], causation_id=root.id, ids={"prompt": uuid.uuid4()})
            with pytest.raises(DBAPIError):
                async with factory.begin() as tx:
                    tx.add(CoreEvent(id=uuid.uuid4(), event_kind="domain", aggregate_type="run", aggregate_id=runs[0],
                        event_type="timeout", run_id=runs[0], causation_id=root.id, payload={"prompt": "bad"}))
                    await tx.flush()
            with pytest.raises(DBAPIError):
                async with factory.begin() as tx:
                    ident = uuid.uuid4()
                    tx.add(CoreEvent(id=ident, event_kind="domain", aggregate_type="run", aggregate_id=runs[0],
                        event_type="timeout", run_id=runs[0], causation_id=ident, payload={}))
                    await tx.flush()
            with pytest.raises(RuntimeError):
                async with factory.begin() as tx:
                    await schedule(tx, runs[0], EventType.TIMEOUT, await db_now(tx), idempotency_key="rollback", causation_id=root.id)
                    raise RuntimeError("rollback")
            async with factory.begin() as tx:
                middle = await emit(tx, EventType.HELPER_REPORT, runs[0], causation_id=root.id)
                end = await emit(tx, EventType.TIMEOUT, runs[0], causation_id=middle.id)
                chain = await causal_chain(tx, end.id, 10)
                assert [row["event_id"] for row in chain["events"]] == [str(end.id), str(middle.id), str(root.id)]
                assert not chain["truncated"]
                assert (await causal_chain(tx, end.id, 1))["truncated"]
                assert await tx.scalar(select(func.count()).select_from(EventTimer).where(EventTimer.run_id == runs[0])) == 0
                timer = await schedule(tx, runs[0], EventType.TIMEOUT, await db_now(tx), idempotency_key="cas", causation_id=root.id)
            with pytest.raises(VersionConflict):
                async with factory.begin() as tx:
                    await cancel(tx, timer.id, 1)
    asyncio.run(scenario())


def test_publish_backoff_visibility_repair_and_metrics():
    async def scenario():
        from backend.core.events.metrics import render_event_metrics
        from backend.core.events.orm import EventAlert
        from backend.core.events.recovery import watchdog
        async with fixture() as (factory, runs):
            root = (await events(factory, runs[0], 0))[0]
            class FakeTransport:
                fail = True
                calls = []
                def send(self, body):
                    self.calls.append(body)
                    if self.fail:
                        raise OSError("broker offline")
            transport = FakeTransport()
            config = settings(publish_alert_attempts=1, delivery_visibility_seconds=.01, backlog_alert_seconds=.001)
            pub = Publisher(factory, config, transport)
            assert not await pub.once()
            async with factory() as tx:
                event = await tx.get(CoreEvent, root.id)
                assert event.delivery_status == "failed" and event.next_attempt_at is not None
                assert await tx.scalar(select(EventAlert).where(EventAlert.event_id == root.id, EventAlert.kind == "publish_failure"))
            transport.fail = False
            await asyncio.sleep(.02)
            assert await pub.once()
            await asyncio.sleep(.02)
            assert await Publisher(factory, config, transport).once()  # receipt absent: repair lost wakeup
            assert len(transport.calls) == 3
            await receive_all(factory, [root])
            await asyncio.sleep(.02)
            assert await pub.claim() is None  # receipt prevents further republishing
            await watchdog(factory, config)
            metrics = "\n".join(await render_event_metrics(factory))
            assert 'aivideo_core_alerts{kind="backlog"}' in metrics
            assert str(root.id) not in metrics
    asyncio.run(scenario())
