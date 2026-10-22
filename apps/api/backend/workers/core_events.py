"""Independent core-event daemons and audited dead-letter administration.

PYTHONPATH=apps/api python -m backend.workers.core_events --help
"""
import argparse
import asyncio
import json
import logging
import os
import signal
from uuid import UUID

from sqlalchemy import select
from backend.core.orm import CoreEvent
from backend.core.events.contracts import EventSettings
from backend.core.events.orm import EventAttempt, EventDeadLetter, EventReceipt, EventQuarantine
from backend.core.events.processor import Processor
from backend.core.events.recovery import watchdog
from backend.core.events.registry import Registry
from backend.core.events.store import causal_chain, replay
from backend.core.events.integrity import replay_quarantine, replay_timer_quarantine
from backend.core.events.locking import bounded
from backend.core.events.timers import Timers
from backend.core.events.transport import Publisher, RabbitTransport, Receiver
from backend.infrastructure.database import create_async_database_engine, create_async_session_factory


async def main(args):
    settings = EventSettings()
    engine = create_async_database_engine()
    factory = create_async_session_factory(engine)
    try:
        if args.role in {"list", "inspect", "replay", "quarantine-replay"}:
            async with factory.begin() as tx:
                await bounded(tx, settings)
                if args.role == "list":
                    rows = (await tx.scalars(select(EventDeadLetter).where(EventDeadLetter.resolved_at.is_(None))
                        .order_by(EventDeadLetter.entered_at).limit(settings.batch_size))).all()
                    quarantines = (await tx.scalars(select(EventQuarantine).where(EventQuarantine.resolved_at.is_(None))
                        .order_by(EventQuarantine.created_at).limit(settings.batch_size))).all()
                    print(json.dumps([{"event_id": str(row.event_id), "error": row.error,
                        "entered_at": row.entered_at.isoformat()} for row in rows] + [{"source": "integrity", "quarantine_id": str(row.id),
                        "event_id": str(row.event_id) if row.event_id else None, "code": row.code,
                        "context": row.context} for row in quarantines], ensure_ascii=False))
                elif args.role == "quarantine-replay":
                    record = await tx.get(EventQuarantine, UUID(args.event_id))
                    if record is None:
                        raise ValueError("unknown quarantine")
                    if record.timer_id is not None:
                        audit = await replay_timer_quarantine(tx, record, operator=args.operator,
                            reason=args.reason, request_key=args.request_key)
                        print(json.dumps(audit, ensure_ascii=False))
                    else:
                        audit = await replay_quarantine(tx, record, operator=args.operator,
                            reason=args.reason, request_key=args.request_key)
                        print(json.dumps({"event_id": str(audit.event_id), "generation": audit.generation}))
                elif args.role == "replay":
                    ident = UUID(args.event_id)
                    record = await tx.scalar(select(EventQuarantine).where(EventQuarantine.source_key == f"event:{ident}"))
                    if record is not None:
                        row = await replay_quarantine(tx, record, operator=args.operator,
                            reason=args.reason, request_key=args.request_key)
                    else:
                        row = await replay(tx, ident, operator=args.operator,
                            reason=args.reason, request_key=args.request_key)
                    if row is None:
                        print(json.dumps({"status": "quarantined", "event_id": str(ident)}))
                        return
                    print(json.dumps({"event_id": str(row.event_id), "generation": row.generation}))
                else:
                    event = await tx.get(CoreEvent, UUID(args.event_id))
                    if event is None:
                        record = await tx.scalar(select(EventQuarantine).where(EventQuarantine.event_id == UUID(args.event_id)))
                        if record is None:
                            raise ValueError("unknown event")
                        print(json.dumps({"status": "quarantined", "code": record.code, "context": record.context}))
                        return
                    receipt = await tx.get(EventReceipt, event.id)
                    record = await tx.scalar(select(EventQuarantine).where(EventQuarantine.source_key == f"event:{event.id}",
                        EventQuarantine.resolved_at.is_(None)))
                    attempts = (await tx.scalars(select(EventAttempt).where(EventAttempt.event_id == event.id)
                        .order_by(EventAttempt.generation, EventAttempt.number))).all()
                    print(json.dumps({"event_id": str(event.id), "run_id": str(event.run_id),
                        "run_sequence": event.run_sequence, "causation_id": str(event.causation_id) if event.causation_id else None,
                        "status": "quarantined" if record else receipt.status if receipt else "unreceived",
                        "integrity": {"code": record.code, "context": record.context} if record else None,
                        "causal_chain": await causal_chain(tx, event.id, settings.causal_max_depth), "attempts": [
                            {"generation": a.generation, "number": a.number, "status": a.status, "error": a.error}
                            for a in attempts]}, ensure_ascii=False))
            return
        transport = RabbitTransport(os.environ["AIVERO_BROKER_URL"], settings) if args.role in {"publisher", "receiver"} else None
        from backend.core.supervisor.engine import Supervisor
        from backend.core.supervisor.contracts import Settings as SupervisorSettings
        supervisor = Supervisor(factory)
        async def supervise():
            return await asyncio.gather(*(supervisor.tick() for _ in range(SupervisorSettings().concurrency)))
        workers = {"supervisor": lambda: supervise, "processor": lambda: Processor(factory, settings, Registry().load(settings.handler_modules)).tick,
            "timer": lambda: Timers(factory, settings).tick,
            "recovery": lambda: lambda: watchdog(factory, settings),
            "publisher": lambda: Publisher(factory, settings, transport).tick,
            "receiver": lambda: Receiver(factory, transport).tick}
        tick = workers[args.role]()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        while not stop.is_set():
            try:
                await tick()
            except Exception:
                logging.exception("core event daemon tick failed: %s", args.role)
            try:
                await asyncio.wait_for(stop.wait(), settings.poll_seconds)
            except TimeoutError:
                pass
    finally:
        await engine.dispose()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="role", required=True)
    for role in ("publisher", "receiver", "processor", "timer", "recovery", "supervisor", "list"):
        commands.add_parser(role)
    for role in ("inspect", "replay", "quarantine-replay"):
        child = commands.add_parser(role)
        child.add_argument("event_id")
        if role in {"replay", "quarantine-replay"}:
            for flag in ("operator", "reason", "request-key"):
                child.add_argument(f"--{flag}", required=True)
    asyncio.run(main(parser.parse_args()))


if __name__ == "__main__":
    cli()
