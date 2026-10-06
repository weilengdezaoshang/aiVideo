"""Authenticated production SSE baseline against explicit isolated PostgreSQL/Redis.

Run durable_read_baseline.py first to migrate the disposable baseline database.
This script seeds only its own synthetic workspaces. It never starts workers or
calls providers. The change test simulates delivery by committing a legal job
transition and publishing the existing Redis notification envelope directly.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
from typing import Any
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api"))
SQL_OWNER: ContextVar[str] = ContextVar("sse_baseline_connection", default="")


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    return {
        "samples": len(ordered),
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
        "max_ms": round(ordered[-1], 3),
        "samples_ms": [round(value, 3) for value in values],
    }


def sql_category(statement: str) -> str:
    lowered = statement.lower()
    for table in ("browser_sessions", "workspace_members", "jobs"):
        if table in lowered:
            return table
    return "other"


@dataclass
class StreamState:
    label: str
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    disconnect: asyncio.Event = field(default_factory=asyncio.Event)
    terminal: asyncio.Event = field(default_factory=asyncio.Event)
    started_ns: int = 0
    snapshot_ns: int | None = None
    snapshot_jobs: int = 0
    snapshot_children: int = 0
    response_status: int | None = None
    received_bytes: int = 0
    ping_ns: list[int] = field(default_factory=list)
    terminal_ns: int | None = None
    expected_job: str | None = None
    error: str | None = None


class TraceRecorder:
    def __init__(self):
        self.rows: list[dict] = []
        self.connections: dict[str, StreamState] = {}
        self.stage = "setup"
        self.scenario = ""

    def emit(self, kind: str, **values):
        self.rows.append({"kind": kind, "scenario": self.scenario, "stage": self.stage,
                          "monotonic_ns": time.perf_counter_ns(),
                          "unix_ns": time.time_ns(), **values})

    def phase(self, owner: str) -> str:
        state = self.connections.get(owner)
        return "initial_snapshot" if state and state.snapshot_ns is None else self.stage

    @contextmanager
    def sql_hooks(self, engine):
        from sqlalchemy import event

        def before(conn, cursor, statement, parameters, context, executemany):
            owner = SQL_OWNER.get()
            if owner:
                context._sse_baseline = {
                    "kind": "sql", "scenario": self.scenario,
                    "stage": self.phase(owner), "connection": owner,
                    "monotonic_ns": time.perf_counter_ns(), "unix_ns": time.time_ns(),
                    "statement": " ".join(statement.split()),
                    "category": sql_category(statement), "executemany": executemany,
                }

        def finish(context, error=None):
            record = getattr(context, "_sse_baseline", None)
            if record is not None:
                record["duration_ms"] = (time.perf_counter_ns() - record["monotonic_ns"]) / 1e6
                if error:
                    record["error"] = error
                self.rows.append(record)
                del context._sse_baseline

        def after(conn, cursor, statement, parameters, context, executemany):
            finish(context)

        def failed(context):
            if context.execution_context is not None:
                finish(context.execution_context, type(context.original_exception).__name__)

        event.listen(engine, "before_cursor_execute", before)
        event.listen(engine, "after_cursor_execute", after)
        event.listen(engine, "handle_error", failed)
        try:
            yield
        finally:
            event.remove(engine, "before_cursor_execute", before)
            event.remove(engine, "after_cursor_execute", after)
            event.remove(engine, "handle_error", failed)


def nested_jobs(job: dict):
    yield job
    for child in job.get("children", []):
        yield from nested_jobs(child)


async def serve_stream(app, token: str, workspace: uuid.UUID, state: StreamState,
                       recorder: TraceRecorder):
    from backend.services.identity import SESSION_COOKIE

    first_receive = True
    buffer = ""

    async def receive():
        nonlocal first_receive
        if first_receive:
            first_receive = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await state.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal buffer
        if message["type"] == "http.response.start":
            state.response_status = message["status"]
            if state.response_status != 200:
                raise RuntimeError(f"SSE returned HTTP {state.response_status}")
        if message["type"] != "http.response.body":
            return
        body = message.get("body", b"")
        state.received_bytes += len(body)
        buffer += body.decode("utf-8")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            now = time.perf_counter_ns()
            if frame.startswith(":"):
                if frame.startswith(": ping"):
                    state.ping_ns.append(now)
                    recorder.emit("sse_ping", connection=state.label)
                continue
            lines = frame.splitlines()
            event_name = next((line[7:] for line in lines if line.startswith("event: ")), "")
            data = "\n".join(line[6:] for line in lines if line.startswith("data: "))
            if not data:
                continue
            payload = json.loads(data)
            if event_name == "snapshot":
                state.snapshot_ns = now
                state.snapshot_jobs = len(payload["jobs"])
                state.snapshot_children = sum(len(job.get("children", [])) for job in payload["jobs"])
                recorder.emit("sse_snapshot", connection=state.label,
                              latency_ms=(now - state.started_ns) / 1e6,
                              jobs=state.snapshot_jobs, children=state.snapshot_children)
                state.ready.set()
            elif event_name == "job":
                recorder.emit("sse_job", connection=state.label, job_id=payload["id"],
                              status=payload["status"], state_version=payload["stateVersion"])
                for job in nested_jobs(payload):
                    if job["id"] == state.expected_job and job["status"] == "failed":
                        state.terminal_ns = now
                        state.terminal.set()
                        recorder.emit("sse_expected_terminal", connection=state.label,
                                      job_id=job["id"], state_version=job["stateVersion"])

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "GET", "scheme": "https",
        "path": "/api/events", "raw_path": b"/api/events", "query_string": b"",
        "root_path": "", "server": ("baseline.test", 443), "client": ("127.0.0.1", 1),
        "headers": [(b"host", b"baseline.test"),
                    (b"cookie", f"{SESSION_COOKIE}={token}".encode()),
                    (b"x-workspace-id", str(workspace).encode())],
    }
    owner = SQL_OWNER.set(state.label)
    state.started_ns = time.perf_counter_ns()
    recorder.emit("sse_connect", connection=state.label)
    try:
        await app(scope, receive, send)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state.error = f"{type(exc).__name__}: {exc}"
        recorder.emit("sse_error", connection=state.label, error=state.error)
        state.ready.set()
    finally:
        recorder.emit("sse_disconnect", connection=state.label)
        SQL_OWNER.reset(owner)


async def seed_workspace(factory, batch: bool, workspace: uuid.UUID):
    from backend.infrastructure.orm import Job, Workspace, WorkspaceMember
    from backend.services.identity import issue_session

    principal = uuid.uuid4().hex
    targets = []
    created = datetime.now(timezone.utc)
    snapshot = {"provider": "mock", "cloudModel": "sse-baseline", "configVersion": "sse-baseline-v1"}
    async with factory.begin() as session:
        session.add(Workspace(id=workspace, name="Synthetic SSE baseline"))
        await session.flush()
        session.add(WorkspaceMember(workspace_id=workspace, principal_id=principal, role="admin"))
        for index in range(100 if batch else 30):
            parent_id = uuid.uuid4()
            params = {"prompt": "synthetic SSE baseline", "model": "sse-baseline",
                      "width": 64, "height": 64, "kind": "image", "batchCount": 4 if batch else 1}
            session.add(Job(id=parent_id, workspace_id=workspace,
                            kind="batch" if batch else "image", status="running",
                            phase="poll", params=params, provider_snapshot=snapshot,
                            state_version=1, execution_epoch=1, progress=0,
                            created_at=created - timedelta(seconds=index), updated_at=created))
            if batch:
                await session.flush()
                for slot in range(4):
                    child = uuid.uuid4()
                    session.add(Job(id=child, workspace_id=workspace, parent_id=parent_id,
                                    kind="image", status="running", phase="poll",
                                    params={**params, "batchCount": 1, "slot": slot},
                                    provider_snapshot=snapshot, state_version=1,
                                    execution_epoch=1, progress=0,
                                    created_at=created - timedelta(seconds=index), updated_at=created))
                    if slot == 0:
                        targets.append((child, parent_id))
            else:
                targets.append((parent_id, None))
    token, _csrf = await issue_session(factory, principal)
    return {"workspace": workspace, "token": token, "targets": targets,
            "parents": 100 if batch else 30, "children": 400 if batch else 0}


async def settle_fixtures(factory, workspaces: list[uuid.UUID]) -> dict:
    """Settle this invocation's fixtures outside measurements, without Outbox events."""
    from sqlalchemy import update
    from backend.infrastructure.orm import Job

    counts = {}
    async with factory.begin() as session:
        for workspace in workspaces:
            now = datetime.now(timezone.utc)
            result = await session.execute(update(Job).where(
                Job.workspace_id == workspace, Job.status.in_(["queued", "running", "unknown"])
            ).values(status="failed", phase="failed", error_code="BASELINE_CLEANUP",
                     recovery="abandon", finished_at=now, updated_at=now,
                     state_version=Job.state_version + 1, next_poll_at=None,
                     dispatched_until=None, slot_scope=None, slot_acquired_at=None,
                     lease_owner=None, lease_until=None))
            counts[str(workspace)] = result.rowcount
    return {"reason": "Settle only this run's synthetic jobs so later chain admission retains its global capacity.",
            "method": "Direct isolated PG update after SQL hooks exit; no Outbox or Redis notification.",
            "jobs_settled": sum(counts.values()), "jobs_settled_by_workspace": counts,
            "measurement_excluded": True}


async def simulate_transition(app, fixture, target, recorder):
    from sqlalchemy import update
    from backend.infrastructure.orm import Job

    job_id, parent_id = target
    owner = SQL_OWNER.set("simulation")
    started = time.perf_counter_ns()
    try:
        async with app.state.factory.begin() as session:
            result = await session.execute(update(Job).where(
                Job.id == job_id, Job.workspace_id == fixture["workspace"], Job.status == "running"
            ).values(status="failed", phase="failed", error_code="UPSTREAM_UNAVAILABLE",
                     recovery="contact", state_version=Job.state_version + 1,
                     finished_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)))
            if result.rowcount != 1:
                raise RuntimeError("Synthetic transition did not update exactly one running job")
            if parent_id is not None:
                # Match the public parent aggregation version bump for a failed child;
                # the other three children remain running, so the parent remains running.
                await session.execute(update(Job).where(Job.id == parent_id).values(
                    state_version=Job.state_version + 1, updated_at=datetime.now(timezone.utc)))
        committed = time.perf_counter_ns()
        recorder.emit("simulation_commit_acknowledged", job_id=str(job_id),
                      parent_id=str(parent_id) if parent_id else None,
                      transaction_ms=(committed - started) / 1e6)
        envelope = {"jobId": str(job_id), "workspaceId": str(fixture["workspace"]), "stateVersion": 2}
        subscribers = await app.state.redis.publish(
            "aivideo:notify:" + str(fixture["workspace"]), json.dumps(envelope))
        published = time.perf_counter_ns()
        recorder.emit("simulation_redis_publish", job_id=str(job_id), subscribers=subscribers)
        return {"mode": "simulation delivery: direct PG transition + Redis publish; no Outbox/broker/worker",
                "job_id": str(job_id), "parent_id": str(parent_id) if parent_id else None,
                "updated_rows": 2 if parent_id else 1, "started_ns": started,
                "commit_acknowledged_ns": committed, "publish_returned_ns": published,
                "redis_subscribers": subscribers}
    finally:
        SQL_OWNER.reset(owner)


def sql_summary(rows: list[dict], labels: set[str], start: int, end: int):
    selected = [row for row in rows if row["kind"] == "sql"
                and row.get("connection") in labels and start <= row["monotonic_ns"] < end]
    duration = (end - start) / 1e9
    counts = Counter(row["connection"] for row in selected)
    return {"duration_s": round(duration, 6), "sql_count": len(selected),
            "sql_per_second": round(len(selected) / duration, 3) if duration else None,
            "sql_per_connection": {label: counts[label] for label in sorted(labels)},
            "sql_per_connection_mean": len(selected) / len(labels),
            "by_table": dict(Counter(row["category"] for row in selected)),
            "sql_duration": distribution([row["duration_ms"] for row in selected])}


async def scenario(app, fixture, count: int, target_index: int, window_s: float,
                   recorder: TraceRecorder, name: str):
    recorder.scenario = f"{name}-{count}-connections"
    recorder.stage = "snapshot_warmup"
    states = [StreamState(f"{recorder.scenario}/connection-{index + 1:02}") for index in range(count)]
    recorder.connections = {state.label: state for state in states}
    tasks = [asyncio.create_task(serve_stream(app, fixture["token"], fixture["workspace"], state, recorder))
             for state in states]
    result: dict[str, Any] = {"scenario": recorder.scenario, "connections": count,
                             "fixture": {key: fixture[key] for key in ("parents", "children")}}
    try:
        await asyncio.wait_for(asyncio.gather(*(state.ready.wait() for state in states)), timeout=60)
        errors = [state.error or f"HTTP {state.response_status}" for state in states
                  if state.error or state.snapshot_ns is None]
        if errors:
            raise RuntimeError("Initial SSE snapshot failed: " + "; ".join(errors))
        recorder.stage = "idle_steady"
        steady_start = time.perf_counter_ns()
        recorder.emit("idle_window_start", connections=count)
        await asyncio.sleep(window_s)
        # The production refresh has 0.8-1.2 jitter plus a 100 ms loop delay.
        # Extend a nominal 12 second sample only when needed to observe two actual
        # refreshes per stream, and report the effective wall time honestly.
        deadline = time.monotonic() + 30
        while any(sum(stamp >= steady_start for stamp in state.ping_ns) < 2 for state in states):
            if any(task.done() for task in tasks) or time.monotonic() >= deadline:
                raise RuntimeError("Not every stream completed two steady-state refreshes")
            await asyncio.sleep(0.1)
        steady_end = time.perf_counter_ns()
        recorder.emit("idle_window_end", connections=count)
        labels = {state.label for state in states}
        initial = [row for row in recorder.rows if row["kind"] == "sql"
                   and row.get("connection") in labels and row["stage"] == "initial_snapshot"]
        initial_counts = Counter(row["connection"] for row in initial)
        result["initial_snapshot"] = {
            "latency": distribution([(state.snapshot_ns - state.started_ns) / 1e6 for state in states]),
            "sql_count": len(initial),
            "sql_per_connection": {state.label: initial_counts[state.label] for state in states},
            "jobs_per_connection": [state.snapshot_jobs for state in states],
            "children_per_connection": [state.snapshot_children for state in states],
        }
        result["idle_steady"] = {
            **sql_summary(recorder.rows, labels, steady_start, steady_end),
            "requested_window_s": window_s,
            "ping_per_connection": {state.label: sum(steady_start <= stamp < steady_end
                                                     for stamp in state.ping_ns) for state in states},
        }
        recorder.stage = "change_delivery"
        target = fixture["targets"][target_index]
        for state in states:
            state.expected_job = str(target[0])
        change_start = time.perf_counter_ns()
        mutation = await simulate_transition(app, fixture, target, recorder)
        try:
            await asyncio.wait_for(asyncio.gather(*(state.terminal.wait() for state in states)), timeout=30)
        except TimeoutError:
            mutation["timed_out"] = True
        change_end = time.perf_counter_ns()
        received = [state for state in states if state.terminal_ns is not None]
        result["change_delivery"] = {
            **mutation, **sql_summary(recorder.rows, labels, change_start, change_end),
            "commit_to_asgi_job_event": distribution([
                (state.terminal_ns - mutation["commit_acknowledged_ns"]) / 1e6 for state in received]),
            "terminal_received": {state.label: state.terminal.is_set() for state in states},
            "all_connections_received_terminal": len(received) == count,
            "simulation_sql_count": sum(row["kind"] == "sql" and row.get("connection") == "simulation"
                                        and change_start <= row["monotonic_ns"] < change_end
                                        for row in recorder.rows),
        }
        result["errors"] = [state.error for state in states if state.error]
        return result
    finally:
        recorder.stage = "shutdown"
        for state in states:
            state.disconnect.set()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def write_results(output: Path, report: dict, recorder: TraceRecorder):
    output.mkdir(parents=True, exist_ok=True)
    (output / "durable-sse.json").write_text(json.dumps(report, indent=2) + "\n")
    with (output / "durable-sse.raw.jsonl").open("w") as stream:
        for row in recorder.rows:
            stream.write(json.dumps(row) + "\n")
    epoch = min((row["monotonic_ns"] for row in recorder.rows), default=0)
    events = []
    for row in recorder.rows:
        entry = {"name": row["kind"] + ("." + row["category"] if row["kind"] == "sql" else ""),
                 "cat": row["scenario"], "pid": os.getpid(), "tid": row.get("connection", "control"),
                 "ts": (row["monotonic_ns"] - epoch) / 1000,
                 "args": {key: value for key, value in row.items()
                          if key not in {"monotonic_ns", "unix_ns", "duration_ms"}}}
        if row["kind"] == "sql":
            entry.update(ph="X", dur=row["duration_ms"] * 1000)
        else:
            entry.update(ph="i", s="t")
        events.append(entry)
    (output / "durable-sse.trace.json").write_text(json.dumps({"traceEvents": events}) + "\n")


async def collect(args):
    from backend.app import create_app
    from backend.infrastructure.database import DatabaseSettings
    from backend.production_settings import ProductionSettings

    database = f"postgresql+psycopg://postgres:baseline@127.0.0.1:{args.db_port}/baseline"
    redis_url = f"redis://127.0.0.1:{args.redis_port}/0"
    saved_env = {key: value for key, value in os.environ.items()
                 if key.startswith(("AIVERO_", "SWARMUI_", "CUTOUT_"))}
    for key in saved_env:
        os.environ.pop(key)
    os.environ["AIVERO_REDIS_URL"] = redis_url
    recorder = TraceRecorder()
    report = {
        "mode": "backend.app:create_app; real isolated PG/Redis; authenticated ASGI SSE",
        "configuration": {"db_host": "127.0.0.1", "db_port": args.db_port, "db_name": "baseline",
                          "redis_host": "127.0.0.1", "redis_port": args.redis_port,
                          "sse_refresh_s": 5, "refresh_jitter": [0.8, 1.2], "loop_sleep_s": 0.1,
                          "pool_size": 2, "max_overflow": 1, "pool_timeout_s": 2,
                          "connections": args.connections, "requested_idle_window_s": args.window_s},
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "scenarios": [],
        "limitations": [
            "ASGI send is the endpoint: excludes socket transfer, proxy buffering, and browser rendering.",
            "Synthetic session issuance exercises normal database authentication; no external OIDC handshake.",
            "State changes are direct isolated PG transitions plus simulated Redis delivery, not full execution.",
            "SQL counts are cursor statements; transaction control and pool pre-ping are not counted.",
            "SQL parameters and session cookies are excluded from trace output.",
            "Scenarios run sequentially with warm shared PG buffers; no concurrent read benchmark.",
            "Created synthetic workspaces remain; their active jobs are settled after measurement to avoid capacity pollution.",
        ],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="aivideo-durable-sse-") as temporary:
            base = Path(temporary)
            routes = base / "routes.json"
            routes.write_text(json.dumps({"image": {"provider": "mock", "cloudModel": "sse-baseline",
                                                    "configVersion": "sse-baseline-v1"}}))
            settings = ProductionSettings(
                public_origin="https://baseline.test", oidc_issuer="https://id.baseline.test",
                oidc_client_id="synthetic-baseline", oidc_client_secret="synthetic-baseline",
                session_secret="synthetic-sse-baseline-session-secret-0000",
                provider_routes_file=routes, media_root=base / "media", sse_refresh_s=5)
            app = create_app(settings, DatabaseSettings(url=database, pool_size=2,
                                                        max_overflow=1, pool_timeout_s=2))
            async with app.router.lifespan_context(app):
                await app.state.redis.ping()
                fixtures, workspaces = {}, []
                try:
                    for name, batch in (("ordinary-30", False), ("batch-100x4", True)):
                        workspace = uuid.uuid4()
                        # Register ownership before seeding: even a session-issuance
                        # failure after seed commit must not leave active fixtures.
                        workspaces.append(workspace)
                        fixtures[name] = await seed_workspace(app.state.factory, batch, workspace)
                    engine = app.state.factory.kw["bind"].sync_engine
                    with recorder.sql_hooks(engine):
                        for name, fixture in fixtures.items():
                            for index, count in enumerate(args.connections):
                                result = await scenario(app, fixture, count, index,
                                                        args.window_s, recorder, name)
                                report["scenarios"].append(result)
                                write_results(args.output, report, recorder)
                                print(json.dumps({"scenario": result["scenario"],
                                                  "idle_sql_per_second": result["idle_steady"]["sql_per_second"],
                                                  "idle_sql_per_connection": result["idle_steady"]["sql_per_connection_mean"],
                                                  "all_terminal_received": result["change_delivery"]["all_connections_received_terminal"]}),
                                      flush=True)
                    report["all_connections_received_terminal"] = all(
                        entry["change_delivery"]["all_connections_received_terminal"] for entry in report["scenarios"])
                finally:
                    recorder.stage = "fixture_cleanup"
                    try:
                        report["fixture_cleanup"] = await settle_fixtures(app.state.factory, workspaces)
                    except Exception as exc:
                        report["fixture_cleanup"] = {"error": f"{type(exc).__name__}: {exc}",
                                                     "workspaces": [str(value) for value in workspaces]}
                        raise
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        write_results(args.output, report, recorder)
        os.environ.pop("AIVERO_REDIS_URL", None)
        os.environ.update(saved_env)
    if not report["all_connections_received_terminal"]:
        raise RuntimeError("At least one stream missed the simulated terminal transition; see durable-sse.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-port", type=int, required=True)
    parser.add_argument("--redis-port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--connections", type=int, nargs="+", default=[1, 10, 30])
    parser.add_argument("--window-s", type=float, default=12)
    args = parser.parse_args()
    if any(not 1024 <= port <= 65535 for port in (args.db_port, args.redis_port)):
        parser.error("explicit high loopback ports for isolated baseline services required")
    if args.window_s < 12 or not args.connections or any(count < 1 for count in args.connections):
        parser.error("window must be at least 12 seconds and connection counts must be positive")
    if len(args.connections) > 30:
        parser.error("at most 30 scenarios per workspace are supported")
    args.output = args.output.resolve()
    asyncio.run(collect(args))


if __name__ == "__main__":
    main()
