"""python -m backend.evaluation.reliability.cli --output /absolute/path

Default provisions and destroys owned Docker infrastructure. Explicit CI services
must use a fresh database named reliability_*; no fallback to application settings.
"""

import argparse
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from .infra import infrastructure
from .runner import load, run


def execute(urls, suite, output):
    return asyncio.run(run(urls, suite, output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="tests/reliability/stage1.yaml")
    parser.add_argument("--output", required=True)
    parser.add_argument("--external", action="store_true")
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="debug one or more named scenarios; not the full CI gate",
    )
    args = parser.parse_args()
    # Real application tuning/credentials never select a provider or alter regression limits.
    for key in tuple(os.environ):
        if key.startswith(
            (
                "AIVERO_CORE_",
                "AIVERO_EXECUTION_",
                "AIVERO_SUPERVISOR_",
                "AIVERO_TOOLS_",
                "AIVERO_DIAGNOSTICS_",
            )
        ):
            os.environ.pop(key)
    suite = load(args.suite)
    if args.case:
        unknown = set(args.case) - {c.id for c in suite.scenarios}
        if unknown:
            raise ValueError("unknown scenario ids: " + ",".join(sorted(unknown)))
        suite = suite.model_copy(
            update={"scenarios": [c for c in suite.scenarios if c.id in args.case]}
        )
    started = time.monotonic()
    if args.external:
        keys = ("AIVERO_RELIABILITY_DB", "AIVERO_RELIABILITY_BROKER", "AIVERO_RELIABILITY_REDIS")
        urls = {k: os.environ[k] for k in keys}
        url = make_url(urls["AIVERO_RELIABILITY_DB"])
        if not (url.database or "").startswith("reliability_") or url.host not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("external mode requires an isolated loopback reliability_* database")
        engine = create_engine(url)
        try:
            if inspect(engine).get_table_names():
                raise ValueError("external database must be EMPTY")
        finally:
            engine.dispose()
        subprocess.run(
            [".venv/bin/alembic", "upgrade", "head"],
            check=True,
            env={
                **os.environ,
                "AIVERO_DB_URL": urls["AIVERO_RELIABILITY_DB"],
                "PYTHONPATH": "apps/api",
            },
            capture_output=True,
        )
        result = execute(urls, suite, args.output)
    else:
        with infrastructure() as urls:
            # Provisioning and migrations count toward the budget; dependency installation does not.
            remaining = suite.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("infrastructure exhausted suite budget")
            result = execute(
                urls, suite.model_copy(update={"timeout_seconds": remaining}), args.output
            )
    result["full_suite"] = not bool(args.case)
    result["total_seconds"] = round(time.monotonic() - started, 3)
    result["within_budget"] = result["total_seconds"] < suite.timeout_seconds
    Path(args.output, "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {k: result[k] for k in ("passed", "failed", "total_seconds", "within_budget")},
            ensure_ascii=False,
        )
    )
    return 0 if result["failed"] == 0 and result["within_budget"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
