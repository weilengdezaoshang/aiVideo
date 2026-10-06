"""Measure the current database API against explicit, empty loopback infrastructure.

Only synthetic baseline credentials are used. This script does not start services,
submit generation, load the historical file-backed application, or flush Redis.
Latency includes ASGI execution and response serialization, but excludes external
HTTP/TLS transport and the correctness checks performed after each response.
"""

import argparse
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api"))
NAMESPACE = uuid.UUID("92c3b3ee-e902-4c73-b571-5a252c9170c8")
FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
METRICS_TOKEN = "synthetic-baseline-metrics-token"
PRINCIPAL = "synthetic-baseline-principal"
POOL = {"pool_size": 2, "max_overflow": 1, "pool_timeout_s": 2}


def ident(label):
    return uuid.uuid5(NAMESPACE, label)


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")


def percentiles(values):
    """Median and nearest-rank P95; raw observations are saved separately."""
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "p50": None, "p95": None, "max": None}
    return {
        "samples": len(ordered),
        "p50": round(statistics.median(ordered), 4),
        "p95": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 4),
        "max": round(ordered[-1], 4),
    }


def isolate_environment(redis_port):
    # BaseSettings and Alembic must not inherit deployment credentials or endpoints.
    for key in list(os.environ):
        if key.startswith(("AIVERO_", "SWARMUI_", "CUTOUT_", "PG", "CELERY_", "REDIS_")):
            del os.environ[key]
    os.environ["AIVERO_REDIS_URL"] = f"redis://127.0.0.1:{redis_port}/15"
    os.environ["CUTOUT_MODEL"] = "baseline-unused"


def migrate_empty_database(port):
    import psycopg
    from alembic import command
    from alembic.config import Config as AlembicConfig

    dsn = (
        f"postgresql://postgres:baseline@127.0.0.1:{port}/baseline"
        "?connect_timeout=2&sslmode=disable"
    )
    with psycopg.connect(dsn) as connection:
        database, user, address = connection.execute(
            "SELECT current_database(), current_user, inet_server_addr()::text"
        ).fetchone()
        if database != "baseline" or user != "postgres":
            raise RuntimeError("Only the synthetic baseline database/user is allowed")
        count = connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        ).fetchone()[0]
        if count:
            raise RuntimeError("Refusing populated public schema; create a fresh baseline database")
        server_version = connection.execute("SHOW server_version").fetchone()[0]
    url = f"postgresql+psycopg://postgres:baseline@127.0.0.1:{port}/baseline?sslmode=disable"
    migration = AlembicConfig(str(ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "apps/api/backend/migrations"))
    migration.set_main_option("sqlalchemy.url", url)
    command.upgrade(migration, "head")
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        heads = [row[0] for row in connection.execute("SELECT version_num FROM alembic_version")]
    return (
        url,
        dsn,
        {"version": server_version, "server_address": address, "migration_heads": heads},
    )


async def seed_foreground(factory):
    from sqlalchemy import insert
    from backend.infrastructure.orm import (
        Document,
        GenerationRequest,
        Job,
        Outbox,
        Workspace,
        WorkspaceMember,
    )

    rng = random.Random(1)
    content = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=100_000))
    workspace, other = ident("workspace"), ident("background-workspace")
    async with factory.begin() as session:
        session.add_all([Workspace(id=workspace), Workspace(id=other)])
        await session.flush()
        session.add(WorkspaceMember(workspace_id=workspace, principal_id=PRINCIPAL, role="admin"))
        for start in range(0, 1000, 100):
            await session.execute(
                insert(Document),
                [
                    {
                        "id": ident(f"document-{i}"),
                        "workspace_id": workspace,
                        "name": f"document-{i}",
                        "revision": 1,
                        "updated_at": FIXTURE_TIME + timedelta(seconds=i),
                        "data": {
                            "objects": {"text": {"id": "text", "kind": "text", "text": content}},
                            "order": ["text"],
                        },
                    }
                    for i in range(start, start + 100)
                ],
            )
        parents = [
            {
                "id": ident(f"parent-{i}"),
                "workspace_id": workspace,
                "document_id": ident(f"document-{i}"),
                "kind": "batch",
                "status": "completed",
                "phase": "done",
                "state_version": 2,
                "params": {"kind": "image", "batchCount": 4, "requestId": f"baseline-request-{i}"},
                "created_at": FIXTURE_TIME + timedelta(seconds=i),
                "finished_at": FIXTURE_TIME + timedelta(seconds=i + 1),
            }
            for i in range(100)
        ]
        await session.execute(insert(Job), parents)
        await session.execute(
            insert(Job),
            [
                {
                    "id": ident(f"child-{i}-{slot}"),
                    "workspace_id": workspace,
                    "document_id": ident(f"document-{i}"),
                    "parent_id": parent["id"],
                    "kind": "image",
                    "status": "completed",
                    "phase": "done",
                    "state_version": 2,
                    "params": {"prompt": "synthetic", "model": "baseline", "batchCount": 1},
                    "created_at": FIXTURE_TIME + timedelta(seconds=i, microseconds=slot),
                    "finished_at": FIXTURE_TIME + timedelta(seconds=i + 1),
                }
                for i, parent in enumerate(parents)
                for slot in range(4)
            ],
        )
        await session.execute(
            insert(GenerationRequest),
            [
                {
                    "id": ident(f"request-{i}"),
                    "workspace_id": workspace,
                    "request_id": f"baseline-request-{i}",
                    "request_hash": hashlib.sha256(f"request-{i}".encode()).hexdigest(),
                    "params": parent["params"],
                    "job_id": parent["id"],
                    "created_at": FIXTURE_TIME + timedelta(seconds=i),
                }
                for i, parent in enumerate(parents)
            ],
        )
        for start in range(0, 10000, 1000):
            await session.execute(
                insert(Outbox),
                [
                    {
                        "id": ident(f"outbox-{i}"),
                        "event_type": "job.changed",
                        "aggregate_type": "job",
                        "aggregate_id": str(ident(f"parent-{i % 100}")),
                        "schema_version": "1",
                        "payload": {},
                        "status": "published",
                        "created_at": FIXTURE_TIME,
                        "published_at": FIXTURE_TIME,
                    }
                    for i in range(start, start + 1000)
                ],
            )
    return content


async def seed_background(factory, previous, total):
    from sqlalchemy import insert
    from backend.infrastructure.orm import Job

    async with factory.begin() as session:
        for start in range(previous, total, 1000):
            await session.execute(
                insert(Job),
                [
                    {
                        "id": ident(f"background-{i}"),
                        "workspace_id": ident("background-workspace"),
                        "kind": "image",
                        "status": "completed",
                        "phase": "done",
                        "params": {"prompt": "synthetic"},
                        "created_at": FIXTURE_TIME,
                        "finished_at": FIXTURE_TIME + timedelta(seconds=1),
                        "slot_scope": "baseline-model" if i % 1000 == 0 else None,
                    }
                    for i in range(start, min(start + 1000, total))
                ],
            )


@dataclass
class RequestTrace:
    request_id: str
    statements: list[dict] = field(default_factory=list)
    pool_checkouts: int = 0


CURRENT_REQUEST: ContextVar[RequestTrace | None] = ContextVar("baseline_request", default=None)


class SqlRecorder:
    def __init__(self, engine):
        from sqlalchemy import event

        self.sql_catalog = {}
        self.unattributed_statements = 0
        self.engine = engine.sync_engine
        self.listeners = [
            ("before_cursor_execute", self.before),
            ("after_cursor_execute", self.after),
            ("handle_error", self.error),
            ("checkout", self.checkout),
        ]
        for name, handler in self.listeners:
            event.listen(self.engine, name, handler)

    def before(self, connection, cursor, statement, parameters, context, executemany):
        context.baseline_started = time.perf_counter_ns()
        context.baseline_trace = CURRENT_REQUEST.get()
        normalized = " ".join(statement.split())
        key = hashlib.sha256(normalized.encode()).hexdigest()[:16]
        self.sql_catalog[key] = normalized
        context.baseline_sql_id = key

    def record(self, context, rowcount, error_type=None):
        trace = getattr(context, "baseline_trace", None)
        if trace is None:
            self.unattributed_statements += 1
            return
        trace.statements.append(
            {
                "sql_id": context.baseline_sql_id,
                "elapsed_ms": (time.perf_counter_ns() - context.baseline_started) / 1e6,
                "cursor_rowcount": rowcount,
                "error_type": error_type,
            }
        )

    def after(self, connection, cursor, statement, parameters, context, executemany):
        self.record(context, cursor.rowcount)

    def error(self, exception_context):
        context = exception_context.execution_context
        if context is not None and hasattr(context, "baseline_started"):
            self.record(context, -1, type(exception_context.original_exception).__name__)

    def checkout(self, connection, record, proxy):
        trace = CURRENT_REQUEST.get()
        if trace is not None:
            trace.pool_checkouts += 1

    def close(self):
        from sqlalchemy import event

        for name, handler in self.listeners:
            event.remove(self.engine, name, handler)


def verify_response(endpoint, response, total, content):
    if response.status_code != 200:
        raise ValueError(f"Unexpected HTTP status {response.status_code}")
    if endpoint == "metrics":
        expected = {
            f'aivideo_jobs{{status="completed"}} {total + 400}',
            'aivideo_outbox{status="published"} 10000',
            f"aivideo_upstream_slots {total // 1000}",
        }
        if not expected.issubset(set(response.text.splitlines())):
            raise ValueError("Metrics fixture mismatch")
        return len([line for line in response.text.splitlines() if not line.startswith("#")])
    body = response.json()
    if endpoint == "session":
        valid = body == {"workspaceId": str(ident("workspace")), "role": "admin"}
        count = 1
    elif endpoint == "documents":
        docs = body.get("documents", [])
        valid = len(docs) == 500 and {row["id"] for row in docs} == {
            str(ident(f"document-{i}")) for i in range(500, 1000)
        }
        valid = valid and all(row.get("objectCount") == 1 for row in docs)
        count = len(docs)
    elif endpoint == "document":
        doc = body.get("document", {})
        valid = (
            doc.get("id") == str(ident("document-0"))
            and doc.get("revision") == 1
            and doc.get("objects", {}).get("text", {}).get("text") == content
        )
        count = 1
    else:
        jobs = body.get("jobs", []) if endpoint == "jobs" else [body.get("job", {})]
        expected = range(100) if endpoint == "jobs" else range(1)
        valid = len(jobs) == len(expected) and {job.get("id") for job in jobs} == {
            str(ident(f"parent-{i}")) for i in expected
        }
        valid = valid and all(
            job.get("status") == "completed"
            and job.get("stateVersion") == 2
            and job.get("batchCount") == 4
            and len(job.get("children", [])) == 4
            for job in jobs
        )
        count = len(jobs) + sum(len(job.get("children", [])) for job in jobs)
    if not valid:
        raise ValueError(f"{endpoint} fixture mismatch")
    return count


async def measure_group(client, endpoint, path, total, concurrency, repeats, content):
    records = []

    async def request(index, warmup):
        request_id = f"{total}-{endpoint}-c{concurrency}-{'warmup' if warmup else 'sample'}-{index}"
        trace = RequestTrace(request_id)
        context_token = CURRENT_REQUEST.set(trace)
        started = time.perf_counter_ns()
        result = {
            "request_id": request_id,
            "warmup": warmup,
            "endpoint": endpoint,
            "path": path,
            "concurrency": concurrency,
            "background_jobs": total,
            "http_status": None,
            "response_bytes": 0,
            "response_items": None,
            "correct": False,
            "error_type": None,
        }
        try:
            response = await client.get(path, headers={"x-request-id": request_id})
            result["latency_ms"] = (time.perf_counter_ns() - started) / 1e6
            result["http_status"] = response.status_code
            result["response_bytes"] = len(response.content)
            result["response_items"] = verify_response(endpoint, response, total, content)
            result["correct"] = True
        except Exception as exc:
            result.setdefault("latency_ms", (time.perf_counter_ns() - started) / 1e6)
            result["error_type"] = type(exc).__name__
            # Do not emit exception text, response bodies, tokens, or SQL parameters.
        finally:
            CURRENT_REQUEST.reset(context_token)
        result.update(
            sql_count=len(trace.statements),
            sql_elapsed_ms=sum(row["elapsed_ms"] for row in trace.statements),
            sql_rows=sum(max(0, row["cursor_rowcount"]) for row in trace.statements),
            sql_unknown_rowcounts=sum(row["cursor_rowcount"] < 0 for row in trace.statements),
            sql_errors=sum(row["error_type"] is not None for row in trace.statements),
            pool_checkouts=trace.pool_checkouts,
            sql=trace.statements,
        )
        records.append(result)

    # One separate wave warms each concurrency profile; no warmup enters percentiles.
    await asyncio.gather(*(request(i, True) for i in range(concurrency)))
    started = time.perf_counter()
    for start in range(0, repeats, concurrency):
        await asyncio.gather(
            *(request(i, False) for i in range(start, min(start + concurrency, repeats)))
        )
    elapsed = time.perf_counter() - started
    measured = [row for row in records if not row["warmup"]]
    correct = [row for row in measured if row["correct"]]
    return records, {
        "endpoint": endpoint,
        "path": path,
        "concurrency": concurrency,
        "measured_requests": len(measured),
        "warmup_requests": len(records) - len(measured),
        "warmup_errors": sum(not row["correct"] for row in records if row["warmup"]),
        "errors": len(measured) - len(correct),
        "error_types": sorted({row["error_type"] for row in measured if row["error_type"]}),
        "http_status_counts": {
            str(status): sum(row["http_status"] == status for row in measured)
            for status in {row["http_status"] for row in measured}
        },
        "all_response_latency_ms": percentiles([row["latency_ms"] for row in measured]),
        "successful_latency_ms": percentiles([row["latency_ms"] for row in correct]),
        "sql_count": percentiles([row["sql_count"] for row in measured]),
        "sql_elapsed_ms": percentiles([row["sql_elapsed_ms"] for row in measured]),
        "sql_rows": percentiles([row["sql_rows"] for row in measured]),
        "response_bytes": percentiles([row["response_bytes"] for row in measured]),
        "measurement_wall_s": elapsed,
        "completed_requests_per_s": len(measured) / elapsed,
        "valid_requests_per_s": len(correct) / elapsed,
    }


def collect_database_state(dsn):
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        # Snapshot before diagnostic queries add their own work to server counters.
        statements = connection.execute(
            "SELECT queryid, query, calls, rows, total_exec_time, mean_exec_time, "
            "min_exec_time, max_exec_time, shared_blks_hit, shared_blks_read, "
            "shared_blks_dirtied, shared_blks_written, local_blks_hit, local_blks_read, "
            "temp_blks_read, temp_blks_written, blk_read_time, blk_write_time "
            "FROM pg_stat_statements WHERE query LIKE 'SELECT%' "
            "AND query NOT LIKE '%pg_stat_statements%' "
            "ORDER BY total_exec_time DESC LIMIT 40"
        ).fetchall()
        plans = {}
        for name, query, parameters in (
            (
                "jobs_active",
                "SELECT * FROM jobs WHERE workspace_id = %s AND parent_id IS NULL "
                "AND status IN ('queued', 'running', 'unknown') ORDER BY created_at DESC",
                (ident("workspace"),),
            ),
            (
                "jobs_finished",
                "SELECT * FROM jobs WHERE workspace_id = %s AND parent_id IS NULL "
                "AND status IN ('completed', 'failed') ORDER BY created_at DESC LIMIT 100",
                (ident("workspace"),),
            ),
            (
                "batch_children",
                "SELECT * FROM jobs WHERE parent_id = %s ORDER BY created_at, id",
                (ident("parent-0"),),
            ),
            (
                "documents_list",
                "SELECT * FROM documents WHERE workspace_id = %s "
                "ORDER BY updated_at DESC LIMIT 500",
                (ident("workspace"),),
            ),
            (
                "slot_scope_count",
                "SELECT count(*) FROM jobs WHERE slot_scope = %s",
                ("baseline-model",),
            ),
            (
                "metrics_status_counts",
                "SELECT status, count(*) FROM jobs WHERE kind != %s GROUP BY status",
                ("batch",),
            ),
        ):
            plans[name] = connection.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, parameters
            ).fetchone()["QUERY PLAN"]
        sizes = connection.execute(
            "SELECT relname, pg_total_relation_size(relid) AS bytes "
            "FROM pg_catalog.pg_statio_user_tables ORDER BY bytes DESC"
        ).fetchall()
        counts = {
            table: connection.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"]
            for table in ("jobs", "documents", "generation_requests", "outbox")
        }
    return {
        "table_rows": counts,
        "relation_sizes": sizes,
        "pg_stat_statements": statements,
        "explain_analyze_buffers": plans,
        "explain_limitation": (
            "EXPLAIN ANALYZE does not send full result rows to the client; JSON/TOAST output, "
            "network transfer, ORM hydration, and HTTP serialization costs differ from endpoint timing."
        ),
    }


async def run(args, output):
    import httpx
    import psycopg
    from redis.asyncio import Redis
    from backend.app import create_app
    from backend.infrastructure.database import (
        DatabaseSettings,
        create_async_database_engine,
        create_async_session_factory,
    )
    from backend.production_settings import ProductionSettings
    from backend.services.identity import SESSION_COOKIE, issue_session

    isolate_environment(args.redis_port)
    redis = Redis.from_url(
        os.environ["AIVERO_REDIS_URL"], socket_connect_timeout=2, socket_timeout=2
    )
    try:
        await redis.ping()
        if await redis.dbsize():
            raise RuntimeError("Refusing nonempty Redis DB 15; provision fresh baseline Redis")
        redis_version = (await redis.info("server"))["redis_version"]
    finally:
        await redis.aclose()
    url, dsn, pg_info = migrate_empty_database(args.db_port)
    database = DatabaseSettings(url=url, **POOL, connect_timeout_s=2, statement_timeout_ms=30_000)
    seed_engine = create_async_database_engine(database)
    seed_factory = create_async_session_factory(seed_engine)
    scenarios = []
    metadata = {
        "schema_version": 1,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "entrypoint": "backend.app:create_app",
        "transport": "httpx ASGITransport, current PostgreSQL API, no external HTTP transport",
        "database": pg_info,
        "redis_version": redis_version,
        "pool": POOL,
        "python": sys.version,
        "platform": platform.platform(),
        "packages": {name: version(name) for name in ("fastapi", "sqlalchemy", "psycopg", "httpx")},
        "fixture": {
            "namespace": str(NAMESPACE),
            "timestamp": FIXTURE_TIME.isoformat(),
            "random_seed": 1,
            "foreground_workspace": str(ident("workspace")),
            "batch_parents": 100,
            "batch_children": 400,
            "documents": 1000,
            "document_text_bytes": 100_000,
            "published_outbox_rows": 10_000,
            "generation_requests": 100,
            "background_jobs": [10_000, 100_000],
            "occupied_slot_rows": "background_jobs / 1000; synthetic aggregate-query load",
            "generated_media": "none; completed fixture jobs intentionally have no asset payload",
        },
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [Path(__file__), *sorted((ROOT / "apps/api/backend").rglob("*.py"))]
        },
        "measurement": {
            "repeats_per_endpoint_and_concurrency": args.repeats,
            "concurrency_levels": [1, 10],
            "load_model": "closed loop; fixed-size waves; no artificial think time",
            "warmup": "one unmeasured wave per endpoint/concurrency after ANALYZE",
            "percentile_method": "P50 median, P95 nearest rank",
            "sql_rows": "sum of nonnegative driver cursor.rowcount; unknown counts are separate",
            "response_bytes": "uncompressed HTTP response body bytes, not PostgreSQL wire bytes",
            "sql_elapsed_ms": "cursor execution only; excludes pool wait and ORM/JSON serialization",
        },
        "limitations": [
            "No legacy application, FIFO scheduler, history.json, or file-backed document store.",
            "Read endpoints only; Celery execution performance is measured by the separate chain run.",
            "Synthetic local data, no concurrent writes, no real OIDC/provider/media traffic.",
            "Warm PostgreSQL/OS cache; this is not a cold-cache experiment or production SLA.",
            "Pool pre-ping/BEGIN/COMMIT protocol traffic is not included in cursor SQL counts.",
            "Query collection adds instrumentation overhead; no SQL parameters or auth tokens saved.",
            "Response correctness runs after latency timing, but is included in group throughput wall.",
        ],
    }
    save(output / "metadata.json", metadata)
    try:
        content = await seed_foreground(seed_factory)
        metadata["fixture"]["document_text_sha256"] = hashlib.sha256(content.encode()).hexdigest()
        save(output / "metadata.json", metadata)
        with tempfile.TemporaryDirectory(prefix="aivideo-durable-read-") as temporary:
            routes = Path(temporary) / "routes.json"
            routes.write_text(
                json.dumps(
                    {
                        "image": {
                            "provider": "mock",
                            "cloudModel": "baseline",
                            "configVersion": "baseline-v1",
                        }
                    }
                )
            )
            settings = ProductionSettings(
                public_origin="https://baseline.test",
                oidc_issuer="https://id.baseline.test",
                oidc_client_id="baseline",
                oidc_client_secret="synthetic",
                session_secret="synthetic-baseline-session-secret-00000000",
                provider_routes_file=routes,
                media_root=Path(temporary) / "media",
                metrics_token=METRICS_TOKEN,
                session_lifetime_s=86400,
                sse_refresh_s=5,
            )
            app = create_app(settings, database)
            async with app.router.lifespan_context(app):
                token, _ = await issue_session(app.state.factory, PRINCIPAL, 86400)
                recorder = SqlRecorder(app.state.factory.kw["bind"])
                try:
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                        base_url="https://baseline.test",
                        headers={
                            "cookie": f"{SESSION_COOKIE}={token}",
                            "authorization": f"Bearer {METRICS_TOKEN}",
                        },
                        trust_env=False,
                    ) as client:
                        previous = 0
                        for total in (10_000, 100_000):
                            await seed_background(seed_factory, previous, total)
                            previous = total
                            with psycopg.connect(dsn, autocommit=True) as connection:
                                connection.execute("ANALYZE")
                                connection.execute("SELECT pg_stat_statements_reset()")
                            scenario = {"background_jobs": total, "groups": []}
                            for endpoint, path in (
                                ("session", "/api/session"),
                                ("jobs", "/api/jobs"),
                                ("job", f"/api/jobs/{ident('parent-0')}"),
                                ("request", "/api/generation-requests/baseline-request-0"),
                                ("documents", "/api/documents"),
                                ("document", f"/api/documents/{ident('document-0')}"),
                                ("metrics", "/internal/metrics"),
                            ):
                                for concurrency in (1, 10):
                                    records, summary = await measure_group(
                                        client,
                                        endpoint,
                                        path,
                                        total,
                                        concurrency,
                                        args.repeats,
                                        content,
                                    )
                                    name = f"{total}-{endpoint}-c{concurrency}.json"
                                    save(output / name, {"summary": summary, "requests": records})
                                    scenario["groups"].append({**summary, "raw_file": name})
                                    save(output / "sql-catalog.json", recorder.sql_catalog)
                                    print(
                                        f"reads: background={total} endpoint={endpoint} "
                                        f"concurrency={concurrency} errors={summary['errors']}",
                                        flush=True,
                                    )
                            scenario["database_state"] = collect_database_state(dsn)
                            scenarios.append(scenario)
                            save(output / "read-summary.json", {"scenarios": scenarios})
                finally:
                    recorder.close()
                    save(output / "sql-catalog.json", recorder.sql_catalog)
                    metadata["unattributed_sql_statements"] = recorder.unattributed_statements
    finally:
        await seed_engine.dispose()
        metadata["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save(output / "metadata.json", metadata)
    errors = sum(
        group["errors"] + group["warmup_errors"] for s in scenarios for group in s["groups"]
    )
    save(output / "read-summary.json", {"errors": errors, "scenarios": scenarios})
    # Collector continues other workloads when a completed read run exposes overload.
    # Unhandled infrastructure, fixture, and program failures retain Python's exit 1.
    return 2 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-port", type=int, required=True)
    parser.add_argument("--redis-port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    if not all(1024 <= port <= 65535 for port in (args.db_port, args.redis_port)):
        parser.error("Ports must be explicit unprivileged loopback ports (1024–65535)")
    if args.repeats < 10:
        parser.error("--repeats must be at least 10 to exercise the concurrent profile")
    output = args.output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("--output must be a new or empty directory")
    output.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(run(args, output)))


if __name__ == "__main__":
    main()
