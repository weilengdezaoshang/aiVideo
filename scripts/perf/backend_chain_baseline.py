"""Real isolated PG/Rabbit/Redis/Celery chain using synthetic Mock providers."""

import argparse
import asyncio
from contextvars import ContextVar
from functools import wraps
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from backend_baseline import ROOT, summary

sys.path.insert(0, str(ROOT / "apps/api"))


def instrument(path):
    from backend.workers import durable
    from backend.providers.staged import StagedProvider
    from backend.infrastructure.artifacts import ArtifactSpool
    from backend.services.execution import ExecutionStore
    from backend.infrastructure import outbox_dispatcher
    from backend.workers.celery_app import celery_app
    from redis.asyncio import Redis

    operation = ContextVar("diagnostic_job", default="")
    lock = threading.Lock()

    def wrap(owner, attribute, name, ident=None):
        original = getattr(owner, attribute)

        def start(args, kwargs):
            job = ident(args, kwargs) if ident else operation.get()
            token = operation.set(str(job))
            return time.time_ns(), time.perf_counter_ns(), str(job), token

        def finish(info):
            wall, monotonic, job, token = info
            record = {
                "name": name,
                "job_id": job,
                "start_unix_ns": wall,
                "duration_ms": (time.perf_counter_ns() - monotonic) / 1e6,
                "pid": os.getpid(),
                "thread": threading.get_ident(),
            }
            with lock, path.open("a") as stream:
                stream.write(json.dumps(record) + "\n")
            operation.reset(token)

        if inspect.iscoroutinefunction(original):

            @wraps(original)
            async def traced(*args, **kwargs):
                info = start(args, kwargs)
                try:
                    return await original(*args, **kwargs)
                finally:
                    finish(info)
        else:

            @wraps(original)
            def traced(*args, **kwargs):
                info = start(args, kwargs)
                try:
                    return original(*args, **kwargs)
                finally:
                    finish(info)

        setattr(owner, attribute, traced)

    wrap(durable, "execute_phase", "worker.execute_phase", lambda args, kwargs: args[0])
    for method in ("claim", "begin_submit", "advance", "complete"):
        wrap(ExecutionStore, method, "execution." + method)
    for method in ("prepare", "submit"):
        wrap(StagedProvider, method, "provider." + method)
    wrap(ArtifactSpool, "save", "artifact.save")
    wrap(
        outbox_dispatcher,
        "publish_event",
        "outbox.publish",
        lambda args, kwargs: kwargs.get("payload", {}).get("jobId", ""),
    )
    wrap(
        celery_app,
        "send_task",
        "bridge.celery_publish",
        lambda args, kwargs: (kwargs.get("args") or [""])[0],
    )

    def redis_job(args, kwargs):
        if len(args) > 2:
            try:
                return json.loads(args[2]).get("jobId", "")
            except (TypeError, ValueError):
                pass
        return ""

    wrap(Redis, "publish", "notify.redis_publish", redis_job)


def component(role, path):
    instrument(path)
    if role.startswith("worker"):
        from backend.workers.celery_app import celery_app

        celery_app.worker_main(
            [
                "worker",
                "--pool=solo",
                "--concurrency=1",
                "-Q",
                "generation.image",
                "--loglevel=warning",
                "--without-gossip",
                "--without-mingle",
                "--hostname=" + role + "@%h",
            ]
        )
    else:
        from backend.workers.daemon import main

        sys.argv = [sys.argv[0], role]
        main()


async def collect(args, output):
    import httpx
    from kombu import Connection
    from sqlalchemy import select
    from backend.infrastructure.database import DatabaseSettings
    from backend.infrastructure.orm import Job, Workspace, WorkspaceMember
    from backend.app import create_app
    from backend.production_settings import ProductionSettings
    from backend.services.identity import SESSION_COOKIE, CSRF_COOKIE, issue_session
    from redis.asyncio import Redis

    database = f"postgresql+psycopg://postgres:baseline@127.0.0.1:{args.db_port}/baseline"
    broker = f"amqp://baseline:baseline@127.0.0.1:{args.broker_port}//"
    redis_url = f"redis://127.0.0.1:{args.redis_port}/0"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SWARMUI_", "AIVERO_", "CUTOUT_"))
    }
    processes, logs = [], []
    with tempfile.TemporaryDirectory(prefix="aivideo-durable-chain-") as temporary:
        base = Path(temporary)
        routes = base / "routes.json"
        routes.write_text(
            json.dumps(
                {
                    "image": {
                        "provider": "mock",
                        "cloudModel": "baseline",
                        "configVersion": "baseline",
                    }
                }
            )
        )
        media = base / "media"
        media.mkdir()
        credentials = base / "credentials.json"
        credentials.write_text("{}")
        env.update(
            PYTHONPATH=str(ROOT / "apps/api"),
            AIVERO_DB_URL=database,
            AIVERO_BROKER_URL=broker,
            AIVERO_REDIS_URL=redis_url,
            SWARMUI_DATA_DIR=str(media),
            AIVERO_CREDENTIALS_FILE=str(credentials),
            SWARMUI_LOG_FORMAT="json",
            LOG_LEVEL="INFO",
            AIVERO_DB_POOL_SIZE="2",
            AIVERO_DB_MAX_OVERFLOW="1",
        )
        settings = ProductionSettings(
            public_origin="https://baseline.test",
            oidc_issuer="https://id.baseline.test",
            oidc_client_id="baseline",
            oidc_client_secret="synthetic",
            session_secret="x" * 40,
            provider_routes_file=routes,
            media_root=media,
        )
        # Application only receives explicit isolated settings; Redis shared with the new notify daemon.
        old_redis = os.environ.get("AIVERO_REDIS_URL")
        os.environ["AIVERO_REDIS_URL"] = redis_url
        app = create_app(settings, DatabaseSettings(url=database, pool_size=2, max_overflow=1))
        try:
            for _ in range(100):
                try:
                    with Connection(broker, connect_timeout=1) as connection:
                        connection.ensure_connection(max_retries=0)
                    break
                except Exception:
                    await asyncio.sleep(0.2)
            else:
                raise RuntimeError("isolated RabbitMQ unavailable")
            for role in ("worker1", "worker2", "dispatcher", "scheduler", "bridge", "notify"):
                path = output / (role + ".spans.jsonl")
                path.write_text("")
                log = (output / (role + ".log")).open("w")
                logs.append(log)
                processes.append(
                    subprocess.Popen(
                        [sys.executable, __file__, "--component", role, "--span-file", str(path)],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                )
            await asyncio.sleep(4)
            async with app.router.lifespan_context(app):
                workspace, principal = uuid.uuid4(), uuid.uuid4().hex
                async with app.state.factory.begin() as session:
                    session.add(Workspace(id=workspace))
                    await session.flush()
                    session.add(
                        WorkspaceMember(
                            workspace_id=workspace, principal_id=principal, role="admin"
                        )
                    )
                token, csrf = await issue_session(app.state.factory, principal)
                redis = Redis.from_url(redis_url)
                pubsub = redis.pubsub()
                await pubsub.subscribe("aivideo:notify:" + str(workspace))
                await pubsub.get_message(timeout=2)
                seen = []
                sse_seen = []
                sse_ready = asyncio.Event()
                sse_disconnect = asyncio.Event()
                first_receive = True

                async def receive():
                    nonlocal first_receive
                    if first_receive:
                        first_receive = False
                        return {"type": "http.request", "body": b"", "more_body": False}
                    await sse_disconnect.wait()
                    return {"type": "http.disconnect"}

                async def send(message):
                    if message["type"] == "http.response.start" and message["status"] != 200:
                        raise RuntimeError("SSE endpoint failed")
                    if message["type"] == "http.response.body":
                        body = message.get("body", b"").decode()
                        if "event: snapshot" in body:
                            sse_ready.set()
                        if "event: job" in body:
                            for line in body.splitlines():
                                if line.startswith("data: "):
                                    payload = json.loads(line[6:])
                                    if payload.get("status") == "completed":
                                        sse_seen.append(
                                            {
                                                "job_id": payload["id"],
                                                "state_version": payload["stateVersion"],
                                                "received_unix_ns": time.time_ns(),
                                            }
                                        )

                sse_scope = {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "https",
                    "path": "/api/events",
                    "raw_path": b"/api/events",
                    "query_string": b"",
                    "root_path": "",
                    "server": ("baseline.test", 443),
                    "client": ("127.0.0.1", 1),
                    "headers": [
                        (b"host", b"baseline.test"),
                        (b"cookie", f"{SESSION_COOKIE}={token}".encode()),
                    ],
                }
                sse_task = asyncio.create_task(app(sse_scope, receive, send))
                await asyncio.wait_for(sse_ready.wait(), timeout=10)

                async def notifications():
                    while True:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=0.2
                        )
                        if message:
                            payload = json.loads(message["data"])
                            seen.append(
                                {
                                    "job_id": payload["jobId"],
                                    "state_version": payload["stateVersion"],
                                    "received_unix_ns": time.time_ns(),
                                }
                            )
                        await asyncio.sleep(0.01)

                observer = asyncio.create_task(notifications())
                try:
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="https://baseline.test",
                        trust_env=False,
                        headers={
                            "origin": settings.public_origin,
                            "x-csrf-token": csrf,
                            "cookie": f"{SESSION_COOKIE}={token}; {CSRF_COOKIE}={csrf}",
                        },
                    ) as client:

                        async def submit():
                            start = time.time_ns()
                            response = await client.post(
                                "/api/generate",
                                json={
                                    "prompt": "synthetic",
                                    "model": "baseline",
                                    "width": 64,
                                    "height": 64,
                                    "seed": 1,
                                    "requestId": uuid.uuid4().hex,
                                },
                            )
                            response.raise_for_status()
                            return {
                                "job_id": response.json()["jobId"],
                                "request_start_unix_ns": start,
                                "accepted_unix_ns": time.time_ns(),
                            }

                        requests = await asyncio.gather(*(submit() for _ in range(4)))
                        identifiers = [uuid.UUID(row["job_id"]) for row in requests]
                        deadline = time.monotonic() + 120
                        while time.monotonic() < deadline:
                            if any(process.poll() is not None for process in processes):
                                raise RuntimeError("chain process exited; inspect diagnostic logs")
                            async with app.state.factory() as session:
                                rows = list(
                                    await session.scalars(
                                        select(Job).where(Job.id.in_(identifiers))
                                    )
                                )
                            if any(row.status in {"failed", "unknown"} for row in rows):
                                raise RuntimeError("synthetic durable job failed")
                            if len(rows) == 4 and all(row.status == "completed" for row in rows):
                                versions = {str(row.id): row.state_version for row in rows}
                                if all(
                                    any(
                                        event["job_id"] == ident
                                        and event["state_version"] >= version
                                        for event in seen
                                    )
                                    and any(
                                        event["job_id"] == ident
                                        and event["state_version"] >= version
                                        for event in sse_seen
                                    )
                                    for ident, version in versions.items()
                                ):
                                    break
                            await asyncio.sleep(0.1)
                        else:
                            raise RuntimeError("chain/notification timed out")
                        result = []
                        spans = []
                        for path in output.glob("*.spans.jsonl"):
                            spans.extend(json.loads(line) for line in path.read_text().splitlines())
                        for request in requests:
                            job = next(row for row in rows if str(row.id) == request["job_id"])
                            started = request["request_start_unix_ns"]
                            finished = int(job.finished_at.timestamp() * 1e9)
                            notification = min(
                                event["received_unix_ns"]
                                for event in seen
                                if event["job_id"] == request["job_id"]
                                and event["state_version"] >= job.state_version
                            )
                            sse_received = min(
                                event["received_unix_ns"]
                                for event in sse_seen
                                if event["job_id"] == request["job_id"]
                                and event["state_version"] >= job.state_version
                            )
                            result.append(
                                {
                                    **request,
                                    "finished_unix_ns": finished,
                                    "notify_unix_ns": notification,
                                    "sse_unix_ns": sse_received,
                                    "completed_to_sse_ms": (sse_received - finished) / 1e6,
                                    "accept_ms": (request["accepted_unix_ns"] - started) / 1e6,
                                    "to_completed_ms": (finished - started) / 1e6,
                                    "completed_to_notify_ms": (notification - finished) / 1e6,
                                }
                            )
                        for row in result:
                            job_spans = [span for span in spans if span["job_id"] == row["job_id"]]
                            workers = sorted(
                                (
                                    span
                                    for span in job_spans
                                    if span["name"] == "worker.execute_phase"
                                ),
                                key=lambda span: span["start_unix_ns"],
                            )
                            row["accepted_to_first_worker_ms"] = (
                                workers[0]["start_unix_ns"] - row["accepted_unix_ns"]
                            ) / 1e6
                            row["worker_phase_total_ms"] = sum(
                                span["duration_ms"] for span in workers
                            )
                            row["provider_total_ms"] = sum(
                                span["duration_ms"]
                                for span in job_spans
                                if span["name"].startswith("provider.")
                            )
                            row["artifact_save_ms"] = sum(
                                span["duration_ms"]
                                for span in job_spans
                                if span["name"] == "artifact.save"
                            )
                            row["between_worker_phases_ms"] = sum(
                                max(
                                    0,
                                    (b["start_unix_ns"] - a["start_unix_ns"]) / 1e6
                                    - a["duration_ms"],
                                )
                                for a, b in zip(workers, workers[1:])
                            )
                        summary_data = {
                            key: summary([row[key] for row in result])
                            for key in (
                                "accept_ms",
                                "to_completed_ms",
                                "completed_to_notify_ms",
                                "completed_to_sse_ms",
                                "accepted_to_first_worker_ms",
                                "worker_phase_total_ms",
                                "provider_total_ms",
                                "artifact_save_ms",
                                "between_worker_phases_ms",
                            )
                        }
                        epoch = min(row["request_start_unix_ns"] for row in result)
                        events = [
                            {
                                "name": span["name"],
                                "ph": "X",
                                "cat": "production",
                                "ts": (span["start_unix_ns"] - epoch) / 1000,
                                "dur": span["duration_ms"] * 1000,
                                "pid": span["pid"],
                                "tid": span["thread"],
                                "args": {"job_id": span["job_id"]},
                            }
                            for span in spans
                        ]
                        for row in result:
                            for name, start, end in [
                                (
                                    "http.accept",
                                    row["request_start_unix_ns"],
                                    row["accepted_unix_ns"],
                                ),
                                (
                                    "request.to_completed",
                                    row["request_start_unix_ns"],
                                    row["finished_unix_ns"],
                                ),
                                (
                                    "completed.to_notify",
                                    row["finished_unix_ns"],
                                    row["notify_unix_ns"],
                                ),
                                (
                                    "completed.to_sse",
                                    row["finished_unix_ns"],
                                    row["sse_unix_ns"],
                                ),
                            ]:
                                events.append(
                                    {
                                        "name": name,
                                        "ph": "X",
                                        "ts": (start - epoch) / 1000,
                                        "dur": (end - start) / 1000,
                                        "pid": os.getpid(),
                                        "tid": row["job_id"],
                                    }
                                )
                        (output / "production-chain.json").write_text(
                            json.dumps(
                                {
                                    "mode": "production ASGI + real PG/Rabbit/Redis; two solo Celery processes; Mock",
                                    "background_jobs": 100000,
                                    "jobs": result,
                                    "notifications": seen,
                                    "sse_completed_events": sse_seen,
                                    "spans": spans,
                                    "summary": summary_data,
                                    "limits": [
                                        "four jobs, synthetic provider, local Docker infrastructure",
                                        "ends at API SSE ASGI send, not browser render/network transfer",
                                        "solo workers do not benchmark prefork hard timeouts",
                                        "job_id correlates spans; no installed production OpenTelemetry SDK",
                                    ],
                                },
                                indent=2,
                            )
                            + "\n"
                        )
                        (output / "production-chain.trace.json").write_text(
                            json.dumps({"traceEvents": events}) + "\n"
                        )
                        print(json.dumps(summary_data), flush=True)
                finally:
                    sse_disconnect.set()
                    try:
                        await asyncio.wait_for(sse_task, timeout=5)
                    except asyncio.TimeoutError:
                        sse_task.cancel()
                        await asyncio.gather(sse_task, return_exceptions=True)
                    observer.cancel()
                    await asyncio.gather(observer, return_exceptions=True)
                    await pubsub.aclose()
                    await redis.aclose()
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for log in logs:
                log.close()
            if old_redis is None:
                os.environ.pop("AIVERO_REDIS_URL", None)
            else:
                os.environ["AIVERO_REDIS_URL"] = old_redis


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--component")
    parser.add_argument("--span-file", type=Path)
    parser.add_argument("--db-port", type=int)
    parser.add_argument("--broker-port", type=int)
    parser.add_argument("--redis-port", type=int)
    parser.add_argument("--output", default="docs/performance/backend-diagnostics-2026-10-04")
    args = parser.parse_args()
    if args.component:
        component(args.component, args.span_file)
        return
    if not all((args.db_port, args.broker_port, args.redis_port)):
        parser.error("isolated db/broker/redis ports required")
    output = ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)
    asyncio.run(collect(args, output))


if __name__ == "__main__":
    main()
