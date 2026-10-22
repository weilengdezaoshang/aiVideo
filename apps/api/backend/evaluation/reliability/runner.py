"""YAML orchestration + universal assertions + machine-readable evidence."""

import asyncio
import json
import time
import traceback
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, ElementTree
import yaml
from sqlalchemy import select, func
from backend.core.diagnostics.invariants import check
from backend.core.diagnostics.reader import report
from backend.core.orm import CoreRun, CoreTask
from backend.core.execution.orm import CanvasWrite
from backend.agent.orm import HostCreditLedger
from .schema import Suite
from .harness import Harness
from .probes import before, after, event_integrity_probe
from .effects import metadata
import uuid
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


def load(path):
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("scenario manifest exceeds 1 MiB")
    return Suite.model_validate(yaml.safe_load(path.read_text()))


async def scenario(urls, root, case, suite):
    h = Harness(urls, root, case, suite)
    try:
        async with h.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        if case.probe == "event_integrity":
            await event_integrity_probe(h)
        await h.setup()
        await before(h)
        await h.drive()
        await after(h)
        await h.events()  # drain the final state event, not only the task result wakeup
        async with h.factory() as tx:
            for ident in h.run_ids:
                run = await tx.get(CoreRun, ident)
                assert run.status == case.expect.run, (case.id, run.status, case.expect.run)
                failures = await check(tx, ident)
                assert not failures, failures
                artifacts = await tx.scalar(
                    select(func.count())
                    .select_from(CanvasWrite)
                    .join(CoreTask, CoreTask.id == CanvasWrite.task_id)
                    .where(CoreTask.run_id == ident, CanvasWrite.history_only.is_(False))
                )
                charged = await tx.scalar(
                    select(func.coalesce(func.sum(HostCreditLedger.charged), 0)).where(
                        HostCreditLedger.core_run_id == ident
                    )
                )
                assert artifacts == (
                    case.expect.canvas_artifacts if ident == h.run_ids[0] else 0
                ), ("canvas", artifacts)
                assert charged == (case.expect.charged if ident == h.run_ids[0] else 0), (
                    "credits",
                    charged,
                )
                actual = {
                    t.task_key: t.status
                    for t in await tx.scalars(select(CoreTask).where(CoreTask.run_id == ident))
                }
                assert all(actual.get(k) == v for k, v in case.expect.tasks.items()), actual
            if case.expect.submissions is not None:
                actual = h.fake.counts()["submissions"] + (
                    h.second.counts()["submissions"] if h.second is not h.fake else 0
                )
                assert actual == case.expect.submissions, ("submissions", actual)
            evidence = await report(tx, await tx.get(CoreRun, h.run_ids[0]))
            (h.root / "trace.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
            return {"run_ids": [str(v) for v in h.run_ids], "invariants": "passed"}
    finally:
        # Run invariant checks even when the scenario's expected-result assertion fails.
        try:
            async with h.factory() as tx:
                checks = {str(ident): await check(tx, ident, settled=True) for ident in h.run_ids}
                (h.root / "invariants.json").write_text(
                    json.dumps(checks, ensure_ascii=False, indent=2)
                )
        finally:
            await h.engine.dispose()


async def run(urls, suite, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    results = []
    started = time.monotonic()
    semaphore = asyncio.Semaphore(suite.concurrency)
    broker_lock = asyncio.Lock()
    source = make_url(urls["AIVERO_RELIABILITY_DB"])
    admin = create_engine(source.set(database="postgres"), isolation_level="AUTOCOMMIT")

    def database(name, create):
        quote = admin.dialect.identifier_preparer.quote_identifier
        with admin.connect() as tx:
            tx.exec_driver_sql(
                f"CREATE DATABASE {quote(name)} TEMPLATE {quote(source.database)}"
                if create
                else f"DROP DATABASE {quote(name)} WITH (FORCE)"
            )

    async def one(case):
        async with semaphore:
            stamp = time.monotonic()
            name = "reliability_" + uuid.uuid4().hex
            result = {"id": case.id, "covers": case.covers}
            created = False
            try:
                await asyncio.to_thread(database, name, True)
                created = True
                own = {
                    **urls,
                    "AIVERO_RELIABILITY_DB": source.set(database=name).render_as_string(
                        hide_password=False
                    ),
                }
                remaining = suite.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("suite deadline exceeded")

                async def invoke():
                    if case.probe in {"queue_backlog", "publisher_crash", "event_integrity"}:
                        async with broker_lock:
                            return await scenario(own, output / case.id, case, suite)
                    return await scenario(own, output / case.id, case, suite)

                data = await asyncio.wait_for(invoke(), min(remaining, suite.case_timeout_seconds))
                result.update(status="passed", **data)
            except Exception as exc:
                result.update(status="failed", error=str(exc), traceback=traceback.format_exc())
            finally:
                if created:
                    await asyncio.to_thread(database, name, False)
            result["seconds"] = round(time.monotonic() - stamp, 3)
            results.append(result)
            print(f"{case.id}: {result['status']} ({result['seconds']}s)", flush=True)
            if result["status"] == "failed":
                print(result["traceback"], flush=True)

    try:
        await asyncio.gather(*(one(case) for case in suite.scenarios))
    finally:
        admin.dispose()
    report_data = {
        "fake_only": True,
        "scenarios": results,
        "seconds": round(time.monotonic() - started, 3),
        "passed": sum(r["status"] == "passed" for r in results),
        "failed": sum(r["status"] != "passed" for r in results),
    }
    (output / "summary.json").write_text(json.dumps(report_data, ensure_ascii=False, indent=2))
    testsuite = Element(
        "testsuite",
        name="stage1-reliability",
        tests=str(len(results)),
        failures=str(report_data["failed"]),
        time=str(report_data["seconds"]),
    )
    for result in results:
        element = SubElement(testsuite, "testcase", name=result["id"], time=str(result["seconds"]))
        if result["status"] != "passed":
            SubElement(element, "failure", message=result["error"]).text = result["traceback"]
    ElementTree(testsuite).write(output / "junit.xml", encoding="utf-8", xml_declaration=True)
    return report_data
