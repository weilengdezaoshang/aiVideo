"""Whitelisted fault probes use production contracts, transactions and transports."""

import asyncio
import uuid
from datetime import timedelta
from sqlalchemy import select, func, update, text
from sqlalchemy.exc import DBAPIError
from backend.core.orm import CoreRun, CoreTask, CoreEvent, CoreAssetVersion
from backend.core.store import transition
from backend.core.states import (
    VersionConflict,
    InvalidTransition,
    RUN_EDGES,
    TASK_EDGES,
    validate_transition,
)
from backend.core.events.store import emit, accept, replay
from backend.core.events.registry import Registry
from backend.core.events.processor import Processor
from backend.core.events.orm import EventReceipt, EventDeadLetter
from backend.core.events.timers import schedule, Timers
from backend.core.events.transport import RabbitTransport, Publisher, Receiver
from backend.core.execution.engine import now


async def cause(h):
    async with h.factory() as tx:
        return await tx.scalar(
            select(CoreEvent.id)
            .where(CoreEvent.run_id == h.run_ids[0])
            .order_by(CoreEvent.run_sequence)
            .limit(1)
        )


async def before(h):
    probe = h.case.probe
    if probe in {"contracts", "tool_contracts"}:
        original = h.case
        names = (
            ("state_edges", "transaction", "optimistic_lock")
            if probe == "contracts"
            else ("typed_rules", "unknown_error")
        )
        try:
            for name in names:
                h.case = original.model_copy(update={"probe": name})
                await before(h)
        finally:
            h.case = original
    elif probe == "migration_cycle":
        await migration_probe(h)
    elif probe == "state_edges":
        import os

        process = await asyncio.create_subprocess_exec(
            ".venv/bin/python",
            "-m",
            "pytest",
            "-q",
            "tests/integration/test_core_contracts.py",
            env={
                **os.environ,
                "PYTHONPATH": "apps/api",
                "AIVERO_CORE_TEST_DB": h.urls["AIVERO_RELIABILITY_DB"],
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await process.communicate()
            (h.root / "core-contracts.log").write_bytes(output)
            assert process.returncode == 0, output.decode(errors="replace")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        for source, targets in RUN_EDGES.items():
            for target in targets:
                validate_transition(
                    "run",
                    source,
                    target,
                    reason="waiting_info" if target == "paused" else None,
                    resume_state=target if source == "paused" else None,
                )
        for source, targets in TASK_EDGES.items():
            for target in targets:
                validate_transition(
                    "task",
                    source,
                    target,
                    task_kind="ask_user" if target == "waiting_user" else "call_tool",
                )
        rejected = 0
        for source in (*RUN_EDGES, "completed", "failed", "cancelled"):
            for target in ("created", "planning", "running", "completed", "failed", "cancelled"):
                if target not in RUN_EDGES.get(source, set()):
                    try:
                        validate_transition("run", source, target)
                    except InvalidTransition:
                        rejected += 1
                    else:
                        raise AssertionError("illegal transition accepted")
        assert rejected >= 10
    elif probe == "optimistic_lock":

        async def writer():
            try:
                async with h.factory.begin() as tx:
                    await transition(tx, "run", h.run_ids[0], 0, "planning")
                return "success"
            except VersionConflict:
                return "conflict"

        assert sorted(await asyncio.gather(writer(), writer())) == ["conflict", "success"]
    elif probe == "transaction":
        original = await cause(h)
        try:
            async with h.factory.begin() as tx:
                await transition(tx, "run", h.run_ids[0], 0, "planning")
                raise RuntimeError("intentional transaction crash")
        except RuntimeError:
            pass
        async with h.factory.begin() as tx:
            run = await tx.get(CoreRun, h.run_ids[0])
            assert run.version == 0 and run.status == "created"
            try:
                await transition(tx, "run", run.id, 0, "planning", causation_id=uuid.uuid4())
            except DBAPIError:
                pass
            else:
                raise AssertionError("missing event FK should roll back transition")
            await tx.refresh(run)
            assert run.status == "created" and await tx.get(CoreEvent, original)
    elif probe == "canvas_conflict":
        from backend.infrastructure.orm import Document
        from backend.core.orm import CoreSession

        async with h.factory.begin() as tx:
            run = await tx.get(CoreRun, h.run_ids[0])
            session = await tx.get(CoreSession, run.session_id)
            doc = await tx.get(Document, session.canvas_id, with_for_update=True)
            import copy

            data = copy.deepcopy(doc.data)
            data["objects"]["source"]["assetVersion"] = 1
            doc.data = data
    elif probe == "typed_rules":
        from backend.agent.admission.tool_types import TypedTask, review_tool_types

        good = [
            TypedTask(
                id="t1",
                tool="edit",
                parameters={
                    "prompt": "test",
                    "image": {"asset_id": str(h.source), "media_type": "image"},
                },
                depends_on=[],
            ),
            TypedTask(
                id="t2",
                tool="video",
                parameters={"prompt": "test", "image": "$t1.output"},
                depends_on=["t1"],
            ),
        ]
        assert review_tool_types(good, h.catalog, h.catalog.lock).valid
        bad = good[1].model_copy(update={"depends_on": []})
        assert not review_tool_types([good[0], bad], h.catalog, h.catalog.lock).valid
    elif probe == "unknown_error":
        from backend.core.tools.errors import normalize_error_durable

        async with h.factory.begin() as tx:
            result = await normalize_error_durable(
                tx, "fake", "never-seen-code", run_id=str(h.run_ids[0])
            )
            assert result.kind == "unknown"
    elif probe in {"event_order", "event_parallel", "dead_replay"}:
        await event_probe(h, probe)
    elif probe in {"timer_restart", "timer_locked"}:
        await timer_probe(h, probe)
    elif probe in {"queue_backlog", "publisher_crash"}:
        await broker_probe(h, probe)
    elif probe == "db_outage":
        await outage_probe(h)
    elif probe in {"planner_limit", "planner_tokens"}:
        async with h.factory.begin() as tx:
            row = await tx.get(CoreRun, h.run_ids[0])
            if probe == "planner_limit":
                row.max_thought_rounds = 1
            else:
                row.tokens_limit = 1


async def restart_wake(h):
    async with h.factory.begin() as tx:
        for ident in h.run_ids:
            first = await tx.scalar(
                select(CoreEvent.id)
                .where(CoreEvent.run_id == ident)
                .order_by(CoreEvent.run_sequence)
                .limit(1)
            )
            await emit(tx, "user_message", ident, causation_id=first)


async def event_probe(h, probe):
    from .effects import effects

    count = h.suite.event_count if probe == "event_order" else h.suite.parallel_runs
    if probe == "event_parallel":
        for _ in range(count - 1):
            await h.new_run()
    async with h.factory.begin() as tx:
        for ident in h.run_ids:
            first = await tx.scalar(
                select(CoreEvent.id)
                .where(CoreEvent.run_id == ident)
                .order_by(CoreEvent.run_sequence)
                .limit(1)
            )
            for _ in range(count if probe == "event_order" else 1):
                await emit(tx, "plan_written", ident, causation_id=first)
        rows = list(
            await tx.scalars(
                select(CoreEvent)
                .where(CoreEvent.run_id.in_(h.run_ids))
                .order_by(CoreEvent.run_sequence)
            )
        )
        for event in reversed(rows):
            for _ in range(2):
                await accept(tx, {"event_id": str(event.id), "run_id": str(event.run_id)})
    active, maximum, per_run = 0, 0, set()
    failing = probe == "dead_replay"

    async def effect(tx, context):
        nonlocal active, maximum
        if context.event.event_type != "plan_written":
            return
        if failing:
            raise RuntimeError("permanent handler failure")
        assert context.run.id not in per_run
        per_run.add(context.run.id)
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(h.suite.poll_seconds)
        await tx.execute(
            effects.insert().values(
                event_id=context.event.id,
                run_id=context.run.id,
                sequence=context.event.run_sequence,
            )
        )
        active -= 1
        per_run.remove(context.run.id)

    registry = Registry()
    from backend.core.events.contracts import EventType

    for kind in EventType:
        registry.register(kind, "reliability-effect-v1", effect)
    settings = h.event_settings.model_copy(
        update={"concurrency": h.suite.parallel_runs, "max_attempts": h.event_settings.max_attempts}
    )
    processor = Processor(h.factory, settings, registry)
    for _ in range(h.suite.max_steps * 2):
        await processor.tick()
        async with h.factory() as tx:
            dead = await tx.scalar(
                select(EventDeadLetter)
                .join(CoreEvent, CoreEvent.id == EventDeadLetter.event_id)
                .where(CoreEvent.run_id.in_(h.run_ids), EventDeadLetter.resolved_at.is_(None))
            )
            remaining = await tx.scalar(
                select(func.count())
                .select_from(EventReceipt)
                .where(EventReceipt.run_id.in_(h.run_ids), EventReceipt.status != "processed")
            )
        if dead:
            assert probe == "dead_replay"
            failing = False
            async with h.factory.begin() as tx:
                await replay(
                    tx,
                    dead.event_id,
                    operator="reliability",
                    reason="handler fixed",
                    request_key=str(uuid.uuid4()),
                )
        if remaining == 0:
            break
        await asyncio.sleep(h.suite.poll_seconds)
    else:
        raise AssertionError("event probe failed to drain")
    if probe == "event_parallel":
        assert maximum > 1
    async with h.factory() as tx:
        for ident in h.run_ids:
            sequences = list(
                await tx.scalars(
                    select(effects.c.sequence)
                    .where(effects.c.run_id == ident)
                    .order_by(effects.c.ordinal)
                )
            )
            assert sequences == sorted(set(sequences))
            assert len(sequences) == (count if probe == "event_order" else 1)
    await finish_probe_runs(h)
    await restart_wake(h)


async def timer_probe(h, probe):
    from backend.core.events.orm import EventTimer

    if probe == "timer_locked":
        for _ in range(5):
            await h.new_run()
    if probe == "timer_restart":
        from .process import kill_checkpoint

        await kill_checkpoint(h, "timer_scheduled", h.run_ids[0])
        h.fault_used = True
        await asyncio.sleep(h.suite.timer_restart_delay_seconds * 2)
        await Timers(h.factory, h.event_settings).tick()
        async with h.factory() as tx:
            timer = await tx.scalar(
                select(EventTimer).where(
                    EventTimer.run_id == h.run_ids[0], EventTimer.idempotency_key == "child"
                )
            )
            assert timer.status == "fired"
        return
    stamp = now() + timedelta(seconds=h.suite.poll_seconds)
    ids = []
    async with h.factory.begin() as tx:
        for ident in h.run_ids:
            first = await tx.scalar(
                select(CoreEvent.id)
                .where(CoreEvent.run_id == ident)
                .order_by(CoreEvent.run_sequence)
                .limit(1)
            )
            timer = await schedule(
                tx, ident, "asset_changed", stamp, idempotency_key="probe", causation_id=first
            )
            ids.append(timer.id)
    await asyncio.sleep(h.suite.poll_seconds * 2)
    if probe == "timer_locked":
        async with h.factory.begin() as lock:
            for ident in h.run_ids[:5]:
                await lock.get(CoreRun, ident, with_for_update=True)
            import time

            until = time.monotonic() + h.suite.timer_lock_seconds
            while time.monotonic() < until:
                await Timers(h.factory, h.event_settings).tick()
                await asyncio.sleep(h.event_settings.timer_backoff_max_seconds)
            async with h.factory() as tx:
                assert (await tx.get(EventTimer, ids[-1])).status == "fired"
                assert (await tx.get(EventTimer, ids[0])).status == "scheduled"
        await asyncio.sleep(h.event_settings.timer_backoff_max_seconds * 2)
    await Timers(h.factory, h.event_settings).tick()  # new service object after "restart"
    async with h.factory() as tx:
        assert all([(await tx.get(EventTimer, ident)).status == "fired" for ident in ids])
    await finish_probe_runs(h)


async def broker_probe(h, probe):
    from .process import kill_checkpoint

    transport = RabbitTransport(h.urls["AIVERO_RELIABILITY_BROKER"], h.event_settings)
    receiver = Receiver(h.factory, transport)
    publisher = Publisher(h.factory, h.event_settings, transport)
    if probe == "publisher_crash":
        await kill_checkpoint(h, "publisher_sent")
        h.fault_used = True
        await asyncio.sleep(h.event_settings.publish_lease_seconds * 2)
    await publisher.tick()
    # Deliberately leave messages queued; PostgreSQL has not accepted/processed them yet.
    async with h.factory() as tx:
        pending = await tx.scalar(
            select(func.count()).select_from(CoreEvent).where(CoreEvent.run_id.in_(h.run_ids))
        )
        assert pending >= 2
    with transport.connection() as connection:
        depth = sum(
            q(connection).queue_declare(passive=True).message_count for q in transport.queues
        )
        assert depth >= 2
    await receiver.tick()


async def outage_probe(h):
    """Terminate owned DB connections through a gated TCP proxy, then recover the same pool."""
    from .proxy import DatabaseProxy
    from backend.infrastructure.database import (
        DatabaseSettings,
        create_async_database_engine,
        create_async_session_factory,
    )
    from sqlalchemy.engine import make_url

    url = make_url(h.urls["AIVERO_RELIABILITY_DB"])
    async with DatabaseProxy(url.host, url.port) as proxy:
        engine = create_async_database_engine(
            DatabaseSettings(url=url.set(port=proxy.port).render_as_string(hide_password=False))
        )
        factory = create_async_session_factory(engine)
        try:
            planned = False
            try:
                async with factory.begin() as tx:
                    await transition(tx, "run", h.run_ids[0], 0, "planning")
                    planned = True
                    await proxy.disconnect()
                    await tx.execute(text("SELECT 1"))
            except Exception:
                assert planned, "outage occurred before the state/outbox write"
            else:
                raise AssertionError("DB outage was not exercised")
            proxy.available = True
            async with factory() as tx:
                row = await tx.get(CoreRun, h.run_ids[0])
                assert row.version == 0 and row.status == "created"
        finally:
            await engine.dispose()


async def after(h):
    if h.case.probe == "tool_contracts":
        original = h.case
        try:
            h.case = original.model_copy(update={"probe": "asset_versions"})
            await after(h)
        finally:
            h.case = original
    if h.case.probe == "diagnostics_contract":
        await diagnostics_probe(h)
    if h.case.probe == "asset_versions":
        from backend.core.store import append_asset_version
        from backend.core.orm import CoreAsset, CoreAssetLineage
        from backend.core.contracts import AssetContent

        async with h.factory.begin() as tx:
            old = await tx.scalar(
                select(CoreAssetVersion)
                .where(CoreAssetVersion.run_id == h.run_ids[0])
                .order_by(CoreAssetVersion.created_at)
                .limit(1)
            )
            asset = await tx.get(CoreAsset, old.asset_id)
            content = AssetContent(**{key: getattr(old, key) for key in AssetContent.model_fields})
            new = await append_asset_version(
                tx,
                asset.id,
                asset.version,
                content.model_copy(update={"starred": True, "content_hash": "d" * 64}),
                parents=(old.id,),
            )
            assert not old.starred and new.previous_version_id == old.id
            assert await tx.scalar(
                select(CoreAssetLineage.output_version_id).where(
                    CoreAssetLineage.output_version_id == new.id
                )
            )
        try:
            async with h.factory.begin() as tx:
                await tx.execute(
                    update(CoreAssetVersion)
                    .where(CoreAssetVersion.id == old.id)
                    .values(starred=True)
                )
        except DBAPIError:
            pass
        else:
            raise AssertionError("old asset version was overwritten")
    if h.case.probe == "mask" and h.case.subjects:
        from PIL import Image
        from backend.infrastructure.orm import Asset

        async with h.factory() as tx:
            task = await tx.scalar(
                select(CoreTask).where(
                    CoreTask.run_id == h.run_ids[0], CoreTask.tool_ref == "cutout"
                )
            )
            asset = await tx.get(Asset, uuid.UUID(task.output["asset_id"]))
        with Image.open(h.root / asset.storage_key) as image:
            assert image.getpixel((0, 0))[3] == 255
            if len(h.case.subjects) > 1:
                both = h.case.choice == "all"
                assert image.getpixel((7, 0))[3] == (255 if both else 0)


async def finish_probe_runs(h):
    # Extra Runs exercise event isolation only; they have no generation tickets or funds.
    async with h.factory.begin() as tx:
        for ident in h.run_ids[1:]:
            row = await tx.get(CoreRun, ident, with_for_update=True)
            for target in ("planning", "running", "completed"):
                await transition(tx, "run", ident, row.version, target)


async def migration_probe(h):
    import os
    import subprocess
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.engine import make_url

    source = make_url(h.urls["AIVERO_RELIABILITY_DB"])
    name = "reliability_migration_" + uuid.uuid4().hex

    def work():
        admin = create_engine(source.set(database="postgres"), isolation_level="AUTOCOMMIT")
        quote = admin.dialect.identifier_preparer.quote_identifier
        try:
            with admin.connect() as tx:
                tx.exec_driver_sql("CREATE DATABASE " + quote(name))
            env = {
                **os.environ,
                "AIVERO_DB_URL": source.set(database=name).render_as_string(hide_password=False),
                "PYTHONPATH": "apps/api",
            }
            logs = []
            for command in (("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")):
                result = subprocess.run(
                    [".venv/bin/alembic", *command],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                logs.append(result.stderr)
            (h.root / "migrations.log").write_text("\n".join(logs))
            engine = create_engine(source.set(database=name))
            try:
                assert "core_planner_invocations" in inspect(engine).get_table_names()
            finally:
                engine.dispose()
        finally:
            with admin.connect() as tx:
                tx.exec_driver_sql("DROP DATABASE IF EXISTS " + quote(name) + " WITH (FORCE)")
            admin.dispose()

    await asyncio.to_thread(work)


async def diagnostics_probe(h):
    from fastapi import FastAPI
    import httpx
    from backend.core.diagnostics.api import install
    from backend.core.diagnostics.reader import page
    from backend.core.diagnostics.metrics import render, monitor
    from backend.core.diagnostics.contracts import Settings
    from backend.core.diagnostics.invariants import check
    from backend.core.events.orm import EventAlert
    from backend.services.identity import Identity, authenticated
    from backend.core.execution.orm import CanvasWrite
    from backend.agent.orm import HostCreditLedger, HostCreditAccount
    from sqlalchemy import delete

    app = FastAPI()
    app.state.factory = h.factory
    install(app)
    app.dependency_overrides[authenticated] = lambda: Identity("reliability", h.workspace, "owner")
    ident = h.run_ids[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/internal/core/runs/{ident}")
        assert response.status_code == 200
        assert "sensitive-marker" not in response.text
        value = response.json()
        assert value["sections"]["plans"]["items"] and value["sections"]["events"]["items"]
        assert value["invariants"] == []
        app.dependency_overrides[authenticated] = lambda: Identity(
            "reliability", h.workspace, "member"
        )
        assert (await client.get(f"/api/internal/core/runs/{ident}")).status_code == 403
        app.dependency_overrides[authenticated] = lambda: Identity(
            "reliability", uuid.uuid4(), "owner"
        )
        assert (await client.get(f"/api/internal/core/runs/{ident}")).status_code == 404
    async with h.factory() as tx:
        run = await tx.get(CoreRun, ident)
        first = await page(tx, run, "events", settings=Settings(page_size=1))
        second = await page(tx, run, "events", first["next"], settings=Settings(page_size=1))
        assert first["items"][0]["id"] != second["items"][0]["id"]
    metrics = "\n".join(await render(h.factory))
    assert str(ident) not in metrics and "aivideo_core_run_success_rate 1.0" in metrics
    assert "aivideo_core_refund_credits 14" in metrics
    await monitor(h.factory, Settings(refund_credits=0))
    await monitor(h.factory, Settings(refund_credits=0))
    async with h.factory() as tx:
        row = await tx.scalar(
            select(EventAlert).where(EventAlert.dedupe_key == "diagnostics:refund_credits")
        )
        assert row and row.occurrences == 1
    await monitor(h.factory, Settings(refund_credits=10000))
    async with h.factory.begin() as tx:
        row = await tx.scalar(
            select(EventAlert).where(EventAlert.dedupe_key == "diagnostics:refund_credits")
        )
        assert row.resolved_at is not None
        savepoint = await tx.begin_nested()
        await tx.execute(
            delete(CanvasWrite).where(
                CanvasWrite.task_id.in_(select(CoreTask.id).where(CoreTask.run_id == ident))
            )
        )
        assert any(e["code"] == "unreceipted_canvas_artifact" for e in await check(tx, ident))
        await savepoint.rollback()
        savepoint = await tx.begin_nested()
        await tx.execute(
            update(HostCreditLedger)
            .where(HostCreditLedger.core_run_id == ident)
            .values(quoted=HostCreditLedger.quoted + 1)
        )
        assert any(e["code"] == "credit_balance" for e in await check(tx, ident))
        await savepoint.rollback()
        savepoint = await tx.begin_nested()
        await tx.execute(
            update(HostCreditAccount)
            .where(HostCreditAccount.workspace_id == h.workspace)
            .values(available=HostCreditAccount.available - 1)
        )
        assert any(e["code"] == "workspace_credit_balance" for e in await check(tx, ident))
        await savepoint.rollback()
        from backend.core.contracts import TaskInput
        from backend.core.store import create_task

        savepoint = await tx.begin_nested()
        root = await tx.get(CoreRun, ident)
        await create_task(
            tx,
            TaskInput(
                run_id=ident,
                plan_id=root.current_plan_id,
                plan_version=1,
                task_key="t99",
                kind="ask_user",
                idempotency_key="negative-control",
                parameters={},
                scope={},
            ),
        )
        assert any(e["code"] == "terminal_run_live_task" for e in await check(tx, ident))
        await savepoint.rollback()


async def event_integrity_probe(h):
    """Run legacy contention/null regressions before creating this scenario's Run."""
    import os

    process = await asyncio.create_subprocess_exec(
        ".venv/bin/python",
        "-m",
        "pytest",
        "-q",
        "tests/integration/test_core_event_lock_recovery.py",
        "-k",
        "not locked_runs_do_not_starve_other_timers",
        env={
            **os.environ,
            "PYTHONPATH": "apps/api",
            "AIVERO_CORE_EVENTS_TEST_DB": h.urls["AIVERO_RELIABILITY_DB"],
            "AIVERO_CORE_EVENTS_TEST_BROKER": h.urls["AIVERO_RELIABILITY_BROKER"],
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await process.communicate()
        (h.root / "event-integrity.log").write_bytes(output)
        assert process.returncode == 0, output.decode(errors="replace")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
