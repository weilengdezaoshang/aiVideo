"""Current durable chain baseline: isolated loopback services and Mock only.

The caller provisions/migrates database ``baseline`` and starts isolated services.
This script never starts containers, purges queues, or changes existing job state.
One real prefork worker uses concurrency=2, matching the image Compose pool.
"""

import argparse
import asyncio
from contextvars import ContextVar
from functools import wraps
import inspect
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
JOB_COUNT = 10

sys.path.insert(0, str(ROOT / "apps/api"))


def summary(values):
    ordered = sorted(values)
    return {
        "samples": len(values),
        "p50_ms": round(statistics.median(values), 2),
        "p95_ms_descriptive_only": round(ordered[math.ceil(0.95 * len(values)) - 1], 2),
        "max_ms": round(max(values), 2),
        "samples_ms": [round(value, 3) for value in values],
    }


def append_record(path, record):
    # One O_APPEND write per bounded record also works across prefork processes.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(record) + "\n").encode())
    finally:
        os.close(descriptor)


def read_spans(output):
    records = []
    for path in output.glob("*.spans.jsonl"):
        for line in path.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # A live writer may have appended only part of its last record.
                continue
    return records


def stop_process_groups(processes):
    # Each Popen below creates its own session: never signal an unrelated PID/group.
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 15
    for process in processes:
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def instrument(path):
    from backend.workers import durable
    from backend.providers.staged import StagedProvider
    from backend.infrastructure.artifacts import ArtifactSpool
    from backend.services.execution import ExecutionStore
    from backend.infrastructure import outbox_dispatcher
    from backend.workers.celery_app import celery_app
    from redis.asyncio import Redis

    operation = ContextVar("diagnostic_job", default="")

    def wrap(owner, attribute, name, ident=None):
        original = getattr(owner, attribute)

        def start(args, kwargs):
            job = ident(args, kwargs) if ident else operation.get()
            token = operation.set(str(job))
            return time.time_ns(), time.perf_counter_ns(), str(job), token

        def finish(info, result, args, error):
            wall, monotonic, job, token = info
            record = {
                "name": name,
                "job_id": job,
                "start_unix_ns": wall,
                "duration_ms": (time.perf_counter_ns() - monotonic) / 1e6,
                "pid": os.getpid(),
                "thread": threading.get_ident(),
                "error_type": error,
            }
            if name == "execution.claim" and result is not None:
                record.update(phase=result.phase, execution_epoch=result.epoch)
            elif name == "execution.advance":
                record["next_phase"] = args[2]
            elif name.startswith("provider.") and result is not None:
                record["next_phase"] = result.phase
            elif name == "worker.execute_phase" and isinstance(result, dict):
                record["outcome"] = result
            append_record(path, record)
            operation.reset(token)

        if inspect.iscoroutinefunction(original):

            @wraps(original)
            async def traced(*args, **kwargs):
                info = start(args, kwargs)
                result, error = None, None
                try:
                    result = await original(*args, **kwargs)
                    return result
                except BaseException as exc:
                    error = type(exc).__name__
                    raise
                finally:
                    finish(info, result, args, error)
        else:

            @wraps(original)
            def traced(*args, **kwargs):
                info = start(args, kwargs)
                result, error = None, None
                try:
                    result = original(*args, **kwargs)
                    return result
                except BaseException as exc:
                    error = type(exc).__name__
                    raise
                finally:
                    finish(info, result, args, error)

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
        from celery.signals import worker_process_init
        from backend.workers.celery_app import celery_app

        def child_started(**kwargs):
            append_record(
                path,
                {
                    "name": "worker.prefork_child_started",
                    "job_id": "",
                    "pid": os.getpid(),
                    "parent_pid": os.getppid(),
                    "thread": threading.get_ident(),
                    "start_unix_ns": time.time_ns(),
                    "duration_ms": 0,
                },
            )

        worker_process_init.connect(child_started, weak=False)
        celery_app.worker_main(
            [
                "worker",
                "--pool=prefork",
                "--concurrency=2",
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
    from backend.providers.policy import ProviderPolicy
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
    configuration = {
        "provider": "mock",
        "credentials": "empty synthetic registry",
        "jobs": JOB_COUNT,
        "workspaces": 1,
        "principals": 1,
        "worker_pool": "prefork",
        "worker_processes": 1,
        "worker_concurrency": 2,
        "daemon_roles": ["dispatcher", "scheduler", "bridge", "notify"],
        "ports": {
            "database": args.db_port,
            "broker": args.broker_port,
            "redis": args.redis_port,
            "management": args.management_port,
        },
        "db_pool_size_per_process": 2,
        "db_max_overflow_per_process": 1,
        "job_status_sample_interval_s": 0.2,
        "queue_sample_interval_s": 0.5,
        "deadline_s": 240,
    }
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
        original_environment = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        configuration["provider_policy"] = ProviderPolicy().model_dump()
        (output / "configuration.json").write_text(json.dumps(configuration, indent=2) + "\n")
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
            for role in ("worker", "dispatcher", "scheduler", "bridge", "notify"):
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
                        start_new_session=True,
                    )
                )
            startup_deadline = time.monotonic() + 30
            while time.monotonic() < startup_deadline:
                if any(process.poll() is not None for process in processes):
                    raise RuntimeError("chain process exited during startup; inspect logs")
                children = {
                    row["pid"]
                    for row in read_spans(output)
                    if row["name"] == "worker.prefork_child_started"
                }
                if len(children) >= 2:
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("two prefork child PIDs were not observed")
            (output / "processes.json").write_text(
                json.dumps(
                    {
                        "owned_process_group_ids": [process.pid for process in processes],
                        "prefork_child_pids": sorted(children),
                    },
                    indent=2,
                )
                + "\n"
            )
            async with app.router.lifespan_context(app):
                workspace, principal = uuid.uuid4(), uuid.uuid4().hex
                configuration.update(workspace_id=str(workspace), principal_id=principal)
                (output / "configuration.json").write_text(
                    json.dumps(configuration, indent=2) + "\n"
                )
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
                queue_samples = []
                job_samples = []
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
                                        event = {
                                            "job_id": payload["id"],
                                            "state_version": payload["stateVersion"],
                                            "received_unix_ns": time.time_ns(),
                                        }
                                        sse_seen.append(event)
                                        append_record(output / "sse-completed-events.jsonl", event)

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
                            event = {
                                "job_id": payload["jobId"],
                                "state_version": payload["stateVersion"],
                                "received_unix_ns": time.time_ns(),
                            }
                            seen.append(event)
                            append_record(output / "redis-notifications.jsonl", event)
                        await asyncio.sleep(0.01)

                observer = asyncio.create_task(notifications())

                async def sample_queues():
                    queue_names = (
                        "aivideo.notifications",
                        "aivideo.phase.events",
                        "generation.image",
                    )

                    def passive():
                        values = {}
                        with Connection(broker, connect_timeout=2) as connection:
                            channel = connection.channel()
                            for name in queue_names:
                                result = channel.queue_declare(queue=name, passive=True)
                                values[name] = {
                                    "ready": result.message_count,
                                    "unacked": None,
                                    "backlog_total": None,
                                    "consumers": result.consumer_count,
                                }
                        return values

                    async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                        while True:
                            sample = {"sampled_unix_ns": time.time_ns(), "queues": {}}
                            try:
                                if args.management_port:
                                    response = await client.get(
                                        f"http://127.0.0.1:{args.management_port}/api/queues/%2F",
                                        auth=("baseline", "baseline"),
                                    )
                                    response.raise_for_status()
                                    for row in response.json():
                                        if row["name"] in queue_names:
                                            sample["queues"][row["name"]] = {
                                                "ready": row.get("messages_ready"),
                                                "unacked": row.get("messages_unacknowledged"),
                                                "backlog_total": row.get("messages"),
                                                "consumers": row.get("consumers"),
                                            }
                                    sample["source"] = "rabbitmq_management_statistics"
                                else:
                                    sample["queues"] = await asyncio.to_thread(passive)
                                    sample["source"] = "amqp_passive_declare_ready_only"
                            except Exception as exc:
                                sample["error_type"] = type(exc).__name__
                            queue_samples.append(sample)
                            append_record(output / "queue-samples.jsonl", sample)
                            await asyncio.sleep(0.5)

                queue_observer = asyncio.create_task(sample_queues())
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

                        requests = await asyncio.gather(*(submit() for _ in range(JOB_COUNT)))
                        (output / "submitted-jobs.json").write_text(
                            json.dumps(requests, indent=2) + "\n"
                        )
                        identifiers = [uuid.UUID(row["job_id"]) for row in requests]
                        deadline = time.monotonic() + 240
                        first_db_complete_seen = {}
                        while time.monotonic() < deadline:
                            if any(process.poll() is not None for process in processes):
                                raise RuntimeError("chain process exited; inspect diagnostic logs")
                            async with app.state.factory() as session:
                                rows = list(
                                    await session.scalars(
                                        select(Job).where(Job.id.in_(identifiers))
                                    )
                                )
                            observed = time.time_ns()
                            for row in rows:
                                snapshot = {
                                    "observed_unix_ns": observed,
                                    "job_id": str(row.id),
                                    "phase": row.phase,
                                    "status": row.status,
                                    "state_version": row.state_version,
                                    "execution_epoch": row.execution_epoch,
                                    "db_finished_unix_ns": int(row.finished_at.timestamp() * 1e9)
                                    if row.finished_at
                                    else None,
                                }
                                job_samples.append(snapshot)
                                append_record(output / "job-state-samples.jsonl", snapshot)
                                if row.status == "completed":
                                    first_db_complete_seen.setdefault(str(row.id), observed)
                            if any(row.status in {"failed", "unknown"} for row in rows):
                                raise RuntimeError("synthetic durable job failed")
                            if len(rows) == JOB_COUNT and all(
                                row.status == "completed" for row in rows
                            ):
                                versions = {str(row.id): row.state_version for row in rows}
                                if (
                                    all(
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
                                    )
                                    and queue_samples
                                    and (
                                        queue_samples[-1]["queues"]
                                        .get("aivideo.notifications", {})
                                        .get("ready")
                                        == 0
                                    )
                                    and (
                                        not args.management_port
                                        or queue_samples[-1]["queues"]
                                        .get("aivideo.notifications", {})
                                        .get("unacked")
                                        == 0
                                    )
                                ):
                                    break
                            await asyncio.sleep(0.2)
                        else:
                            raise RuntimeError("chain/notification timed out")
                        result = []
                        spans = read_spans(output)
                        executing_children = {
                            span["pid"] for span in spans if span["name"] == "worker.execute_phase"
                        }
                        if not executing_children or not executing_children.issubset(children):
                            raise RuntimeError(
                                "execution spans do not prove prefork child execution"
                            )
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
                                    "db_completion_first_observed_unix_ns": first_db_complete_seen[
                                        request["job_id"]
                                    ],
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
                                    "mode": "current durable ASGI + real PG/Rabbit/Redis; one prefork Celery worker concurrency=2; Mock",
                                    "configuration": configuration,
                                    "jobs": result,
                                    "notifications": seen,
                                    "redis_completed_events": [
                                        event
                                        for event in seen
                                        if event["job_id"] in versions
                                        and event["state_version"] >= versions[event["job_id"]]
                                    ],
                                    "sse_completed_events": sse_seen,
                                    "job_state_samples": job_samples,
                                    "queue_samples": queue_samples,
                                    "prefork_child_pids": sorted(children),
                                    "executing_child_pids": sorted(executing_children),
                                    "spans": spans,
                                    "summary": summary_data,
                                    "limits": [
                                        "ten jobs, synthetic Mock provider, loopback infrastructure; P95 is descriptive only",
                                        "ends at API SSE ASGI send, not browser render/network transfer",
                                        "prefork process topology is real; no worker-loss or hard-timeout injection",
                                        "management queue counts reflect RabbitMQ statistics refresh cadence, not instantaneous counters",
                                        "without management port, passive declare exposes ready only; unacked/total are null",
                                        "DB finished_at is application timestamp; separately recorded polling observation bounds commit visibility",
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
                    queue_observer.cancel()
                    await asyncio.gather(observer, queue_observer, return_exceptions=True)
                    await pubsub.aclose()
                    await redis.aclose()
        finally:
            await asyncio.to_thread(stop_process_groups, processes)
            for log in logs:
                log.close()
            os.environ.clear()
            os.environ.update(original_environment)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--component")
    parser.add_argument("--span-file", type=Path)
    parser.add_argument("--db-port", type=int)
    parser.add_argument("--broker-port", type=int)
    parser.add_argument("--redis-port", type=int)
    parser.add_argument("--management-port", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.component:
        if (
            args.component not in {"worker", "dispatcher", "scheduler", "bridge", "notify"}
            or args.span_file is None
        ):
            parser.error("internal component requires a known role and --span-file")
        component(args.component, args.span_file)
        return
    if not all((args.db_port, args.broker_port, args.redis_port, args.output)):
        parser.error("explicit isolated db/broker/redis ports and output required")
    if any(
        not 1024 < value <= 65535
        for value in (args.db_port, args.broker_port, args.redis_port, args.management_port)
        if value is not None
    ):
        parser.error("use explicit nonprivileged loopback ports between 1025 and 65535")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("output directory must be empty, to prevent mixing baseline runs")

    def interrupted(signum, frame):
        raise KeyboardInterrupt("baseline interrupted; cleaning owned process groups")

    signal.signal(signal.SIGTERM, interrupted)
    asyncio.run(collect(args, output))


if __name__ == "__main__":
    main()
