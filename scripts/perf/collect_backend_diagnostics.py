"""Run all diagnostic harnesses with dedicated disposable Docker infrastructure."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]


def command(arguments):
    return subprocess.check_output(arguments, cwd=ROOT, text=True).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/performance/backend-diagnostics-2026-10-04")
    args = parser.parse_args()
    output = ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("Choose an empty output directory; existing baselines must not be overwritten")
    suffix = uuid.uuid4().hex[:10]
    containers = []

    def container(role, image, port, options):
        name = f"aivideo-baseline-{role}-{suffix}"
        command(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "--name",
                name,
                "-p",
                f"127.0.0.1::{port}",
                *options,
                image,
                *(
                    [
                        "-c",
                        "shared_preload_libraries=pg_stat_statements",
                        "-c",
                        "track_io_timing=on",
                        "-c",
                        "log_min_duration_statement=20",
                    ]
                    if role == "postgres"
                    else []
                ),
            ]
        )
        containers.append(name)
        assigned = command(["docker", "port", name, f"{port}/tcp"])
        return name, assigned.rsplit(":", 1)[1]

    def run(script, extra=()):
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/perf" / script), "--output", str(output), *extra],
            cwd=ROOT,
            check=True,
        )

    try:
        run("backend_diagnostics.py")
        postgres, db_port = container(
            "postgres",
            "postgres:16",
            5432,
            ["-e", "POSTGRES_PASSWORD=baseline", "-e", "POSTGRES_DB=baseline"],
        )
        run("backend_sql_baseline.py", ["--port", db_port])
        _, redis_port = container("redis", "redis:7-alpine", 6379, [])
        _, broker_port = container(
            "rabbit",
            "rabbitmq:3.13-alpine",
            5672,
            ["-e", "RABBITMQ_DEFAULT_USER=baseline", "-e", "RABBITMQ_DEFAULT_PASS=baseline"],
        )
        run(
            "backend_chain_baseline.py",
            ["--db-port", db_port, "--redis-port", redis_port, "--broker-port", broker_port],
        )
        reads = []
        with subprocess.Popen(
            ["docker", "logs", postgres],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as logs:
            for line in logs.stdout:
                match = re.search(
                    r"duration: ([0-9.]+) ms\s+(?:statement:|execute [^:]*:)\s*(.*)", line
                )
                if match and match[2].lstrip().upper().startswith(("SELECT", "EXPLAIN", "WITH")):
                    reads.append(
                        {"duration_ms": float(match[1]), "statement_start": match[2][:1500]}
                    )
            if logs.wait():
                raise RuntimeError("Cannot read temporary PostgreSQL logs")
        (output / "postgres-slow-reads.json").write_text(
            json.dumps(
                {
                    "threshold_ms": 20,
                    "records": reads,
                    "note": "Read statement first lines only; bind parameters omitted.",
                },
                indent=2,
            )
            + "\n"
        )
        scripts = sorted((ROOT / "scripts/perf").glob("*backend*.py"))
        manifest = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git_revision": command(["git", "rev-parse", "HEAD"]),
            "git_dirty": bool(command(["git", "status", "--porcelain"])),
            "docker_server": command(["docker", "version", "--format", "{{.Server.Version}}"]),
            "images": ["postgres:16", "redis:7-alpine", "rabbitmq:3.13-alpine"],
            "scripts": {
                str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in scripts
            },
            "data_files": {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in output.iterdir()
                if path.is_file()
            },
            "no_paid_calls": True,
            "sse_boundary": "ASGI send; browser rendering not exercised",
        }
        (output / "environment.json").write_text(json.dumps(manifest, indent=2) + "\n")
    finally:
        for name in reversed(containers):
            subprocess.run(["docker", "rm", "-f", name], check=False, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
