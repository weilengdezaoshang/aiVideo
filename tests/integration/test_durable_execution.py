"""Opt-in isolated Postgres/Redis tests. Never fall back to repository config."""

import asyncio
from datetime import timedelta
import os
import uuid

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import update

from backend.errors import ErrorCode
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Job
from backend.infrastructure.resilience import CircuitDeferred, RedisResilience
from backend.providers.policy import Certainty, Operation, ProviderFailure, ProviderPolicy
from backend.services.acceptance import accept_generation
from backend.services.execution import ExecutionStore, utcnow
from backend.services.generation_runner import execute_phase

DB = os.environ.get("AIVERO_DURABLE_TEST_DB")
REDIS = os.environ.get("AIVERO_DURABLE_TEST_REDIS")
pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="explicit isolated DB/Redis URLs required")


async def setup():
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    return engine, create_async_session_factory(engine)


async def accept(factory, *, snapshot=None, request_id=None, workspace=None, policy=None):
    return await accept_generation(factory, workspace_id=workspace or uuid.uuid4(),
        request_id=request_id or uuid.uuid4().hex, request_hash="same",
        params={"prompt": "test", "model": "mock-test", "width": 64, "height": 64, "steps": 1},
        provider_snapshot=snapshot or {"provider": "mock"}, policy=policy)


def test_spooled_result_recovers_after_execution_deadline(tmp_path):
    from backend.infrastructure.artifacts import ArtifactSpool
    from backend.providers.base import Generated

    async def scenario():
        engine, factory = await setup()
        try:
            job_id = (await accept(factory))["jobId"]
            store = ExecutionStore(factory)
            first = await store.claim(job_id)
            await store.advance(first, "submit", {})
            claim = await store.claim(job_id)
            assert await store.begin_submit(claim, uuid.uuid4().hex, 1)
            spool = ArtifactSpool(tmp_path)
            spool.save(job_id, claim.epoch, Generated(b"recovered-media", "png"))
            async with factory.begin() as session:
                await session.execute(update(Job).where(Job.id == job_id).values(
                    lease_until=utcnow() - timedelta(seconds=1),
                    execution_deadline=utcnow() - timedelta(seconds=1)))
            # Redelivery must not bypass the receipt-aware recovery scanner.
            assert await store.claim(job_id) is None
            await store.recover_expired(spool=spool)
            recovered = await store.claim(job_id)
            assert recovered is not None and recovered.phase == "persist"
            assert recovered.deadline > utcnow()
            assert await store.complete(recovered, recovered.payload)
            assert not await store.complete(claim, recovered.payload)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_concurrent_same_request_returns_one_job():
    async def scenario():
        engine, factory = await setup()
        try:
            workspace, request = uuid.uuid4(), uuid.uuid4().hex
            results = await asyncio.gather(*(accept(factory, workspace=workspace, request_id=request) for _ in range(12)))
            assert len({r["jobId"] for r in results}) == 1
            assert sum(not r["replayed"] for r in results) == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_queue_timeout_never_submits():
    async def scenario():
        engine, factory = await setup()
        try:
            job_id = (await accept(factory))["jobId"]
            async with factory.begin() as session:
                await session.execute(update(Job).where(Job.id == job_id).values(
                    queue_deadline=utcnow() - timedelta(seconds=1)))
            assert await ExecutionStore(factory).claim(job_id) is None
            async with factory() as session:
                row = await session.get(Job, job_id)
                assert row.status == "failed" and row.error_code == "QUEUE_TIMEOUT"
                assert row.attempt_count == 0 and row.slot_scope is None
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_scheduler_deadlines_without_broker_preserve_unknown_slot():
    async def scenario():
        engine, factory = await setup()
        try:
            queued = (await accept(factory))["jobId"]
            submitted = (await accept(factory))["jobId"]
            store = ExecutionStore(factory)
            claim = await store.claim(submitted)
            await store.advance(claim, "submit", {})
            claim = await store.claim(submitted)
            scope = uuid.uuid4().hex
            assert await store.begin_submit(claim, scope, 1)
            await store.advance(claim, "poll", {}, external_id="original-upstream")
            past = utcnow() - timedelta(seconds=1)
            async with factory.begin() as session:
                await session.execute(update(Job).where(Job.id == queued).values(queue_deadline=past))
                await session.execute(update(Job).where(Job.id == submitted).values(execution_deadline=past))
            await store.expire_due()
            async with factory() as session:
                first, second = await session.get(Job, queued), await session.get(Job, submitted)
                assert first.status == "failed" and first.error_code == "QUEUE_TIMEOUT"
                assert second.status == "unknown" and second.phase == "reconcile"
                assert second.slot_scope == scope and second.external_task_id == "original-upstream"
                deadline = second.execution_deadline
            async with factory.begin() as session:
                await session.execute(update(Job).where(Job.id == submitted).values(next_poll_at=utcnow()))
            claim = await store.claim(submitted)
            assert claim.phase == "reconcile"
            await store.advance(claim, "poll", {}, delay_s=30)
            async with factory() as session:
                second = await session.get(Job, submitted)
                assert second.execution_deadline == deadline and second.slot_scope == scope
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_idempotent_replay_precedes_outbox_backpressure():
    from backend.errors import QueueFullError
    async def scenario():
        engine, factory = await setup()
        try:
            workspace, request = uuid.uuid4(), uuid.uuid4().hex
            result = await accept(factory, workspace=workspace, request_id=request)
            policy = ProviderPolicy(outbox_capacity=1)
            replay = await accept(factory, workspace=workspace, request_id=request, policy=policy)
            assert replay["replayed"] and replay["jobId"] == result["jobId"]
            with pytest.raises(QueueFullError):
                await accept(factory, policy=policy)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_poll_retry_exhaustion_keeps_original_handle_and_slot():
    async def scenario():
        engine, factory = await setup()
        try:
            job_id = (await accept(factory))["jobId"]
            store = ExecutionStore(factory, ProviderPolicy(max_retries=1))
            first = await store.claim(job_id)
            await store.advance(first, "submit", {})
            submitted = await store.claim(job_id)
            await store.begin_submit(submitted, uuid.uuid4().hex, 1)
            await store.advance(submitted, "poll", {}, external_id="existing-upstream")
            failure = ProviderFailure(ErrorCode.UPSTREAM_TIMEOUT, operation=Operation.POLL,
                certainty=Certainty.UNKNOWN, retryable=True, retry_after=0)
            for _ in range(2):
                claim = await store.claim(job_id)
                assert claim.phase == "poll" and claim.deadline == first.deadline
                assert await store.fail(claim, failure)
            async with factory() as session:
                row = await session.get(Job, job_id)
                assert row.status == "unknown" and row.slot_scope is not None
                assert row.external_task_id == "existing-upstream" and row.attempt_count == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_unknown_submit_retains_slot_and_deadline():
    async def scenario():
        engine, factory = await setup()
        try:
            job_id = (await accept(factory))["jobId"]
            store = ExecutionStore(factory)
            first = await store.claim(job_id)
            await store.advance(first, "submit", {})
            second = await store.claim(job_id)
            assert first.deadline == second.deadline
            assert await store.begin_submit(second, uuid.uuid4().hex, 1)
            failure = ProviderFailure(ErrorCode.UPSTREAM_TIMEOUT, operation=Operation.SUBMIT, certainty=Certainty.UNKNOWN)
            assert await store.fail(second, failure)
            async with factory() as session:
                job = await session.get(Job, job_id)
                assert job.status == "unknown" and job.slot_scope is not None
                assert job.execution_deadline == first.deadline
            assert await store.claim(job_id) is None
            assert not await store.advance(first, "completed", {})
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_unknown_reconciliation_never_resets_execution_deadline():
    async def scenario():
        engine, factory = await setup()
        try:
            job_id = (await accept(factory))["jobId"]
            store = ExecutionStore(factory, ProviderPolicy(max_retries=0))
            prepared = await store.claim(job_id)
            await store.advance(prepared, "submit", {})
            submitted = await store.claim(job_id)
            await store.begin_submit(submitted, uuid.uuid4().hex, 1)
            await store.advance(submitted, "poll", {}, external_id="original")
            polling = await store.claim(job_id)
            await store.fail(polling, ProviderFailure(ErrorCode.UPSTREAM_TIMEOUT,
                operation=Operation.POLL, retryable=True))
            async with factory.begin() as session:
                await session.execute(update(Job).where(Job.id == job_id).values(next_poll_at=utcnow()))
            reconcile = await store.claim(job_id)
            assert reconcile.phase == "reconcile" and reconcile.external_task_id == "original"
            await store.advance(reconcile, "poll", {}, external_id="original")
            async with factory() as session:
                row = await session.get(Job, job_id)
                assert row.status == "unknown" and row.phase == "reconcile"
                assert row.execution_deadline == submitted.deadline
                assert row.reconcile_deadline == reconcile.deadline
                assert row.slot_scope and row.attempt_count == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_expired_worker_is_fenced_and_no_blind_resubmit():
    async def scenario():
        engine, factory = await setup()
        try:
            job_id = (await accept(factory))["jobId"]
            store = ExecutionStore(factory)
            first = await store.claim(job_id)
            await store.advance(first, "submit", {})
            claim = await store.claim(job_id)
            await store.begin_submit(claim, uuid.uuid4().hex, 1)
            async with factory.begin() as session:
                await session.execute(update(Job).where(Job.id == job_id).values(lease_until=utcnow() - timedelta(seconds=1)))
            assert await store.recover_expired() >= 1
            assert not await store.advance(claim, "poll", {}, external_id="late")
            async with factory() as session:
                job = await session.get(Job, job_id)
                assert job.status == "unknown" and job.external_task_id is None
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_real_phase_runner_mock_completes_without_network(tmp_path):
    async def scenario():
        engine, factory = await setup()
        redis = Redis.from_url(REDIS)
        try:
            job_id = (await accept(factory))["jobId"]
            def no_network(request):
                raise AssertionError("mock must never access paid upstream")
            async with httpx.AsyncClient(transport=httpx.MockTransport(no_network)) as client:
                prepared = await execute_phase(job_id, factory, client, redis, tmp_path, {}, set())
                assert prepared["phase"] == "submit"
                done = await execute_phase(job_id, factory, client, redis, tmp_path, {}, set())
                assert done["completed"]
            async with factory() as session:
                job = await session.get(Job, job_id)
                assert job.status == "completed" and job.slot_scope is None
                assert (tmp_path / job.phase_payload["storage_key"]).is_file()
        finally:
            await redis.aclose()
            await engine.dispose()
    asyncio.run(scenario())


def test_shared_breaker_single_probe_and_stale_result_ignored():
    async def scenario():
        redis = Redis.from_url(REDIS)
        scope = uuid.uuid4().hex
        controls = [RedisResilience(redis, ProviderPolicy(breaker_min_calls=2)) for _ in range(2)]
        try:
            old = await controls[0].acquire(scope, 10)
            for control in controls:
                permit = await control.acquire(scope, 10)
                await control.record(permit, "failure")
            with pytest.raises(CircuitDeferred):
                await controls[0].acquire(scope, 10)
            await controls[0].record(old, "success")
            with pytest.raises(CircuitDeferred):
                await controls[0].acquire(scope, 10)
            await redis.hset(controls[0].keys(scope)[0], "until", "0")
            results = await asyncio.gather(*(control.acquire(scope, 10) for control in controls), return_exceptions=True)
            assert sum(isinstance(x, CircuitDeferred) for x in results) == 1
            probe = next(x for x in results if not isinstance(x, Exception))
            await controls[0].record(probe, "success")
            await controls[1].acquire(scope, 10)
        finally:
            await redis.delete(*controls[0].keys(scope))
            await redis.aclose()
    asyncio.run(scenario())


def test_malformed_completed_image_is_not_published_or_resubmitted(tmp_path):
    async def scenario():
        engine, factory = await setup()
        redis = Redis.from_url(REDIS)
        calls = []
        reference = uuid.uuid4().hex
        try:
            job_id = (await accept(factory, snapshot={"provider": "cloud", "cloudVendor": "openai",
                "cloudBaseUrl": "https://isolated.invalid", "cloudModel": "mock-test",
                "credentialRef": reference}))["jobId"]
            def provider(request):
                calls.append(request)
                return httpx.Response(200, json={"data": [{"b64_json": "bm90LWFuLWltYWdl"}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                credentials = {reference: {"imageApiKey": "local-test-only"}}
                await execute_phase(job_id, factory, client, redis, tmp_path, credentials, set())
                assert (await execute_phase(job_id, factory, client, redis, tmp_path, credentials, set()))["failed"] == "INVALID_ARTIFACT"
                assert (await execute_phase(job_id, factory, client, redis, tmp_path, credentials, set()))["skipped"]
            async with factory() as session:
                row = await session.get(Job, job_id)
                assert row.status == "failed" and row.slot_scope is None
                assert "assetId" not in row.phase_payload
            assert len(calls) == 1 and not list(tmp_path.rglob("receipt.json"))
        finally:
            await redis.aclose()
            await engine.dispose()
    asyncio.run(scenario())


def test_hundred_jobs_never_exceed_five_model_slots():
    async def scenario():
        engine, factory = await setup()
        policy = ProviderPolicy(global_capacity=10000, workspace_capacity=200)
        store = ExecutionStore(factory, policy)
        scope, workspace = uuid.uuid4().hex, uuid.uuid4()
        try:
            accepted = await asyncio.gather(*(accept(factory, workspace=workspace, policy=policy) for _ in range(100)))
            semaphore = asyncio.Semaphore(8)
            async def submit_one(result):
                async with semaphore:
                    claim = await store.claim(result["jobId"])
                    await store.advance(claim, "submit", {})
                    claim = await store.claim(result["jobId"])
                    return await store.begin_submit(claim, scope, 5)
            outcomes = await asyncio.gather(*(submit_one(r) for r in accepted))
            assert sum(outcomes) == 5
            async with factory.begin() as session:
                await session.execute(update(Job).where(Job.workspace_id == workspace).values(
                    status="failed", lease_owner=None, lease_until=None,
                    slot_scope=None, slot_acquired_at=None))
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_outbox_bridge_real_celery_phase_worker(tmp_path, monkeypatch):
    broker = os.environ.get("AIVERO_DURABLE_TEST_BROKER")
    if not broker:
        pytest.skip("explicit isolated RabbitMQ URL required")
    import time
    from celery.contrib.testing.worker import start_worker
    from kombu import Connection
    from backend.infrastructure.messaging import declare_topology
    from backend.infrastructure.outbox_dispatcher import OutboxDispatcher
    from backend.workers.celery_app import celery_app
    from backend.workers.daemon import bridge_once, schedule_once
    from backend.workers.runtime import get_runtime

    monkeypatch.setenv("AIVERO_DB_URL", DB)
    monkeypatch.setenv("AIVERO_REDIS_URL", REDIS)
    monkeypatch.setenv("SWARMUI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(celery_app.conf, "broker_url", broker)
    runtime = get_runtime(DatabaseSettings(url=DB))
    async def seed():
        return (await accept(runtime.session_factory()))["jobId"]
    job_id = runtime.run(seed(), timeout=10)
    async def status():
        async with runtime.session_factory()() as session:
            return (await session.get(Job, job_id)).status
    async def schedule():
        return await schedule_once(runtime.session_factory(), tmp_path)
    dispatcher = OutboxDispatcher(DatabaseSettings(url=DB), broker)
    try:
        with start_worker(celery_app, perform_ping_check=False, pool="solo",
                          queues=["generation.image"], loglevel="error"):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                runtime.run(schedule(), timeout=10)
                dispatcher.dispatch_once(limit=1000)
                with Connection(broker, transport_options={"confirm_publish": True}) as conn:
                    declare_topology(conn)
                    bridge_once(conn, runtime, limit=1000)
                if runtime.run(status(), timeout=10) == "completed":
                    break
                time.sleep(.1)
            assert runtime.run(status(), timeout=10) == "completed"
    finally:
        runtime.close()
