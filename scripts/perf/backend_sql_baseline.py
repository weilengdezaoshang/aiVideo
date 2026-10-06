"""Production read-path SQL baseline against a freshly created localhost baseline DB."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import time
import uuid

from backend_baseline import ROOT, summary

sys.path.insert(0, str(ROOT / "apps/api"))


def save(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


async def run(port, output):
    import httpx
    from alembic import command
    from alembic.config import Config as AlembicConfig
    import psycopg
    from sqlalchemy import event, insert
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from backend.infrastructure.database import DatabaseSettings
    from backend.infrastructure.orm import Document, Job, Outbox, Workspace, WorkspaceMember
    from backend.app import create_app
    from backend.production_settings import ProductionSettings
    from backend.services.identity import SESSION_COOKIE, issue_session

    url = f"postgresql+psycopg://postgres:baseline@127.0.0.1:{port}/baseline"
    dsn = f"postgresql://postgres:baseline@127.0.0.1:{port}/baseline"
    # Refuse any populated database; only use the fresh profiling container.
    for _ in range(100):
        try:
            with psycopg.connect(dsn) as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
                    ).fetchone()[0]
                    == 0
                ), "Use fresh empty profiling DB"
            break
        except psycopg.OperationalError:
            await asyncio.sleep(0.1)
    else:
        raise RuntimeError("profiling DB unavailable")
    os.environ.pop("AIVERO_DB_URL", None)
    migration = AlembicConfig(str(ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "apps/api/backend/migrations"))
    migration.set_main_option("sqlalchemy.url", url)
    command.upgrade(migration, "head")
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("CREATE EXTENSION pg_stat_statements")
    engine = create_async_engine(url, pool_size=5, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    workspace = uuid.uuid4()
    other = uuid.uuid4()
    principal = "baseline-user"
    rng = random.Random(1)
    content = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=100000))
    async with factory.begin() as session:
        session.add_all([Workspace(id=workspace), Workspace(id=other)])
        await session.flush()
        session.add(WorkspaceMember(workspace_id=workspace, principal_id=principal, role="admin"))
    with tempfile.TemporaryDirectory(prefix="aivideo-sql-") as temporary:
        routes = Path(temporary) / "routes.json"
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
        settings = ProductionSettings(
            public_origin="https://baseline.test",
            oidc_issuer="https://id.baseline.test",
            oidc_client_id="baseline",
            oidc_client_secret="synthetic",
            session_secret="x" * 40,
            provider_routes_file=routes,
            media_root=Path(temporary) / "media",
        )
        app = create_app(settings, DatabaseSettings(url=url, pool_size=5, max_overflow=0))
        async with app.router.lifespan_context(app):
            token, _ = await issue_session(app.state.factory, principal)
            api_engine = app.state.factory.kw["bind"]
            sql_samples = []
            current_request = "idle"

            @event.listens_for(api_engine.sync_engine, "before_cursor_execute")
            def before(connection, cursor, statement, parameters, context, executemany):
                context.baseline_start = time.perf_counter_ns()

            @event.listens_for(api_engine.sync_engine, "after_cursor_execute")
            def after(connection, cursor, statement, parameters, context, executemany):
                sql_samples.append(
                    {
                        "request": current_request,
                        "duration_ms": (time.perf_counter_ns() - context.baseline_start) / 1e6,
                        "sql": " ".join(statement.split()),
                        "rowcount": cursor.rowcount,
                    }
                )

            results = []
            for total in (10000, 100000):
                previous = 0 if total == 10000 else 10000
                async with factory.begin() as session:
                    for start in range(previous, total, 1000):
                        await session.execute(
                            insert(Job),
                            [
                                {
                                    "id": uuid.uuid4(),
                                    "workspace_id": other,
                                    "kind": "image",
                                    "status": "completed",
                                    "params": {"prompt": "synthetic"},
                                    "message": "done",
                                    "slot_scope": "baseline-model" if i % 1000 == 0 else None,
                                }
                                for i in range(start, min(start + 1000, total))
                            ],
                        )
                    if total == 10000:
                        now = datetime.now(timezone.utc)
                        parents = [
                            {
                                "id": uuid.uuid4(),
                                "workspace_id": workspace,
                                "kind": "batch",
                                "status": "completed",
                                "params": {"kind": "image"},
                                "created_at": now + timedelta(seconds=i),
                            }
                            for i in range(100)
                        ]
                        await session.execute(insert(Job), parents)
                        await session.execute(
                            insert(Job),
                            [
                                {
                                    "id": uuid.uuid4(),
                                    "workspace_id": workspace,
                                    "parent_id": parent["id"],
                                    "kind": "image",
                                    "status": "completed",
                                    "params": {"prompt": "synthetic"},
                                }
                                for parent in parents
                                for _ in range(4)
                            ],
                        )
                        for start in range(0, 1000, 100):
                            await session.execute(
                                insert(Document),
                                [
                                    {
                                        "id": uuid.uuid4(),
                                        "workspace_id": workspace,
                                        "name": f"document-{i}",
                                        "revision": 1,
                                        "data": {
                                            "objects": {"text": {"text": content}},
                                            "order": ["text"],
                                        },
                                    }
                                    for i in range(start, start + 100)
                                ],
                            )
                        await session.execute(
                            insert(Outbox),
                            [
                                {
                                    "id": uuid.uuid4(),
                                    "event_type": "job.changed",
                                    "aggregate_type": "job",
                                    "aggregate_id": str(uuid.uuid4()),
                                    "schema_version": "1",
                                    "payload": {},
                                    "status": "published",
                                }
                                for _ in range(10000)
                            ],
                        )
                with psycopg.connect(dsn, autocommit=True) as connection:
                    connection.execute("ANALYZE")
                    connection.execute("SELECT pg_stat_statements_reset()")
                sql_samples.clear()
                requests = []
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://baseline.test",
                    headers={"cookie": f"{SESSION_COOKIE}={token}"},
                    trust_env=False,
                ) as client:
                    for path in ("/api/documents", "/api/jobs"):
                        timings, queries = [], []
                        for index in range(11):
                            current_request = f"{total}:{path}:{index}"
                            before_count = len(sql_samples)
                            start = time.perf_counter()
                            response = await client.get(path)
                            response.raise_for_status()
                            elapsed = (time.perf_counter() - start) * 1000
                            assert response.json()
                            if index:
                                timings.append(elapsed)
                                queries.append(len(sql_samples) - before_count)
                        requests.append(
                            {"path": path, "wall": summary(timings), "sql_count_samples": queries}
                        )
                current_request = "idle"
                with psycopg.connect(dsn, autocommit=True) as connection:
                    # Actual SQLAlchemy SQL retained, parameters intentionally omitted.
                    connection.row_factory = psycopg.rows.dict_row
                    stats = connection.execute("""SELECT query, calls, rows, total_exec_time, mean_exec_time,
                        max_exec_time, shared_blks_hit, shared_blks_read, temp_blks_written,
                        blk_read_time, blk_write_time FROM pg_stat_statements
                        WHERE query LIKE 'SELECT%' ORDER BY total_exec_time DESC LIMIT 30""").fetchall()
                    plans = {}
                    for name, query, params in [
                        (
                            "slots",
                            "SELECT count(*) FROM jobs WHERE slot_scope = %s",
                            ("baseline-model",),
                        ),
                        (
                            "metrics",
                            "SELECT status, count(*) FROM jobs WHERE kind != %s GROUP BY status",
                            ("batch",),
                        ),
                        (
                            "documents",
                            "SELECT * FROM documents WHERE workspace_id = %s ORDER BY updated_at DESC LIMIT 500",
                            (workspace,),
                        ),
                    ]:
                        plans[name] = connection.execute(
                            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params
                        ).fetchone()["QUERY PLAN"]
                    sizes = connection.execute(
                        "SELECT relname, pg_total_relation_size(relid) AS bytes FROM pg_catalog.pg_statio_user_tables ORDER BY bytes DESC"
                    ).fetchall()
                save(
                    output / f"sql-{total}.json",
                    {
                        "background_jobs": total,
                        "batch_parents": 100,
                        "children": 400,
                        "documents": 1000,
                        "document_text_bytes": 100000,
                        "request_metrics": requests,
                        "sqlalchemy_cursor_samples": sql_samples,
                        "pg_stat_statements": stats,
                        "explain_analyze_buffers": plans,
                        "relation_sizes": sizes,
                        "slow_query_threshold_ms": 20,
                        "slow_cursor_samples": [
                            row for row in sql_samples if row["duration_ms"] >= 20
                        ],
                    },
                )
                results.append(
                    {
                        "background_jobs": total,
                        "requests": requests,
                        "slot_plan_execution_ms": plans["slots"][0]["Execution Time"],
                        "metrics_plan_execution_ms": plans["metrics"][0]["Execution Time"],
                        "documents_plan_execution_ms": plans["documents"][0]["Execution Time"],
                    }
                )
                print("SQL collected:", total, flush=True)
            save(
                output / "sql-summary.json",
                {
                    "mode": "real PostgreSQL 16; production ASGI read endpoints",
                    "threshold_ms": 20,
                    "scenarios": results,
                    "limitations": [
                        "localhost isolated fixtures, warm cache, 10 measured requests per endpoint",
                        "cursor duration excludes ORM hydration/response serialization; endpoint wall includes them",
                        "no concurrent writes/replication/real network latency",
                    ],
                },
            )
    await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", default="docs/performance/backend-diagnostics-2026-10-04")
    args = parser.parse_args()
    output = ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(args.port, output))


if __name__ == "__main__":
    main()
