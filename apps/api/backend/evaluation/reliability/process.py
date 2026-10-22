"""Real, bounded SIGKILL checkpoints, isolated to an explicitly spawned child."""

import asyncio
import multiprocessing
import os
import signal
import uuid
from pathlib import Path
from backend.core.execution.fake import FakeProvider, Scenario
from backend.core.execution.engine import Worker
from backend.core.execution.contracts import WorkerSettings
from backend.core.supervisor.engine import Supervisor, register
from backend.core.supervisor.planners import FakePlanner
from backend.core.events.contracts import EventSettings
from backend.core.events.processor import Processor
from backend.core.events.registry import Registry
from backend.core.events.transport import Publisher, RabbitTransport
from backend.infrastructure.database import (
    DatabaseSettings,
    create_async_database_engine,
    create_async_session_factory,
)


class KillAfterAcceptance(FakeProvider):
    async def submit(self, *args, **kwargs):
        await super().submit(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGKILL)


def child(urls, root, worker, events, script, behavior, checkpoint, job_id, timer_delay):
    async def work():
        engine = create_async_database_engine(DatabaseSettings(url=urls["AIVERO_RELIABILITY_DB"]))
        factory = create_async_session_factory(engine)
        if checkpoint in {"plan", "planner"}:
            await Supervisor(factory, FakePlanner(script)).tick()
        elif checkpoint == "wake":
            registry = Registry()
            register(registry)
            processor = Processor(factory, EventSettings(**events), registry)
            # Consume both creation and user-message notifications before killing.
            for _ in range(2):
                await processor.tick()
        elif checkpoint == "timer_scheduled":
            from sqlalchemy import select
            from datetime import timedelta
            from backend.core.events.timers import schedule
            from backend.core.execution.engine import now
            from backend.core.orm import CoreEvent

            async with factory.begin() as tx:
                run_id = uuid.UUID(job_id)
                first = await tx.scalar(
                    select(CoreEvent.id)
                    .where(CoreEvent.run_id == run_id)
                    .order_by(CoreEvent.run_sequence)
                    .limit(1)
                )
                await schedule(
                    tx,
                    run_id,
                    "asset_changed",
                    now() + timedelta(seconds=timer_delay),
                    idempotency_key="child",
                    causation_id=first,
                )
        elif checkpoint == "publisher_sent":

            async def die(_):
                os.kill(os.getpid(), signal.SIGKILL)

            settings = EventSettings(**events)
            await Publisher(
                factory,
                settings,
                RabbitTransport(urls["AIVERO_RELIABILITY_BROKER"], settings),
                after_send=die,
            ).once()
        else:
            cls = KillAfterAcceptance if checkpoint == "provider_accepted" else FakeProvider
            provider = cls(Path(root) / "provider.sqlite", Scenario(**behavior))
            await Worker(factory, Path(root), provider, settings=WorkerSettings(**worker)).execute(
                uuid.UUID(job_id)
            )
        os.kill(os.getpid(), signal.SIGKILL)

    asyncio.run(work())


async def kill_checkpoint(harness, checkpoint, job_id=None):
    process = multiprocessing.get_context("spawn").Process(
        target=child,
        args=(
            harness.urls,
            str(harness.root),
            harness.worker_settings.model_dump(),
            harness.event_settings.model_dump(),
            harness.script,
            harness.case.provider.model_dump(),
            checkpoint,
            str(job_id) if job_id else None,
            harness.suite.timer_restart_delay_seconds,
        ),
    )
    process.start()
    try:
        await asyncio.to_thread(process.join, harness.suite.case_timeout_seconds)
        assert process.exitcode == -signal.SIGKILL, (
            f"checkpoint child failed: {checkpoint}, exit={process.exitcode}"
        )
    finally:
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join)
        process.close()
