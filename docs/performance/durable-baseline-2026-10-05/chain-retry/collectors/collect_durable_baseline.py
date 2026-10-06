"""Measure only the current database application using disposable local services.

Never loads application .env files, credentials, data/ or the historical sandbox.
The infrastructure is created here; child collectors receive only its loopback ports.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = "durable-backend-baseline-v1"


def command(arguments, *, timeout=60):
    return subprocess.check_output(arguments, cwd=ROOT, text=True, timeout=timeout).strip()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def fingerprints():
    paths = set((ROOT / "apps/api/backend").rglob("*.py"))
    paths.update(
        ROOT / name
        for name in (
            "alembic.ini",
            "requirements.txt",
            "requirements.lock.txt",
            "compose.execution.yaml",
            "compose.production.yaml",
            "Dockerfile",
        )
    )
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
        if path.is_file()
    }


def wait_port(port, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"Isolated service not reachable on loopback port {port}")


def stop_child(process):
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument(
        "--stages", nargs="+", choices=("reads", "sse", "chain"), default=["reads", "sse", "chain"]
    )
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("Output must be empty; previous baselines are never overwritten")
    if args.repeats < 10:
        parser.error("At least 10 measured read samples are required")
    for name in (
        "durable_read_baseline.py",
        "durable_sse_baseline.py",
        "durable_chain_baseline.py",
    ):
        if not (ROOT / "scripts/perf" / name).is_file():
            parser.error(f"Missing collector: {name}")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AIVERO_", "SWARMUI_", "CUTOUT_"))
    }
    env.update(PYTHONPATH=str(ROOT / "apps/api"), PYTHONUNBUFFERED="1")
    source_before = fingerprints()
    suffix = uuid.uuid4().hex[:10]
    containers = []
    stages = []
    started = datetime.now(timezone.utc).isoformat()
    manifest = {
        "protocol": PROTOCOL,
        "started_at_utc": started,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "load_average_before": os.getloadavg(),
        "git_revision": command(["git", "rev-parse", "HEAD"]),
        "git_dirty": bool(command(["git", "status", "--porcelain"])),
        "backend_source_sha256": source_before,
        "collector_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "scripts/perf").glob("*durable*baseline.py"))
        },
        "docker_version": command(["docker", "version", "--format", "{{.Server.Version}}"]),
        "other_running_container_count": len(command(["docker", "ps", "-q"]).splitlines()),
        "paid_provider_calls": False,
        "application_entrypoint": "backend.app:create_app",
        "transport": "Production ASGI application; no external HTTP/TLS/OIDC round trip",
        "cache_mode": "existing application; no added cache or performance fixes",
        "limitations": [
            "Local shared macOS host; other containers were not stopped",
            "Warm database/filesystem caches; synthetic data and Mock provider only",
            "Read/SSE SQL hooks and chain span instrumentation add overhead",
            "SSE endpoint is ASGI send, not browser arrival or rendering",
        ],
    }
    if sys.platform == "darwin":
        manifest["cpu_model"] = command(["sysctl", "-n", "machdep.cpu.brand_string"])
        manifest["memory_bytes"] = int(command(["sysctl", "-n", "hw.memsize"]))
    save(output / "environment.json", manifest)
    snapshot = output / "collectors"
    snapshot.mkdir()
    for path in sorted((ROOT / "scripts/perf").glob("*durable*baseline.py")):
        shutil.copyfile(path, snapshot / path.name)
    manifest["requested_stages"] = args.stages

    def interrupt(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)

    def container(role, image, ports, options=(), extra=()):
        name = f"aivideo-durable-baseline-{role}-{suffix}"
        arguments = [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "--label",
            f"aivideo.baseline.run={suffix}",
        ]
        for port in ports:
            arguments.extend(["-p", f"127.0.0.1::{port}"])
        command([*arguments, *options, image, *extra])
        containers.append(name)
        mapped = {
            port: int(command(["docker", "port", name, f"{port}/tcp"]).rsplit(":", 1)[1])
            for port in ports
        }
        return name, mapped

    def collect(name, script, extra):
        destination = output / name
        destination.mkdir()
        print(f"Collecting {name}", flush=True)
        begin = time.monotonic()
        arguments = [
            sys.executable,
            str(ROOT / "scripts/perf" / script),
            "--output",
            str(destination),
            *extra,
        ]
        process = None
        with (output / f"{name}.log").open("w") as log:
            try:
                process = subprocess.Popen(
                    arguments,
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                code = process.wait()
            finally:
                if process is not None:
                    stop_child(process)
        stages.append(
            {"name": name, "elapsed_seconds": time.monotonic() - begin, "exit_code": code}
        )
        save(output / "stages.json", stages)
        if code and not (name == "reads" and code == 2):
            raise RuntimeError(f"{name} failed; inspect {name}.log (exit {code})")
        print(f"Finished {name} in {stages[-1]['elapsed_seconds']:.1f}s", flush=True)

    try:
        pg_name, pg = container(
            "postgres",
            "postgres:16-alpine",
            [5432],
            ["-e", "POSTGRES_PASSWORD=baseline", "-e", "POSTGRES_DB=baseline"],
            [
                "-c",
                "shared_preload_libraries=pg_stat_statements",
                "-c",
                "track_io_timing=on",
                "-c",
                "log_min_duration_statement=20",
            ],
        )
        _, redis_ports = container(
            "redis",
            "redis:7-alpine",
            [6379],
            extra=[
                "redis-server",
                "--appendonly",
                "yes",
                "--maxmemory",
                "256mb",
                "--maxmemory-policy",
                "noeviction",
            ],
        )
        mq_name, mq = container(
            "rabbitmq",
            "rabbitmq:3.13-alpine",
            [5672, 15672],
            ["-e", "RABBITMQ_DEFAULT_USER=baseline", "-e", "RABBITMQ_DEFAULT_PASS=baseline"],
        )
        wait_port(pg[5432])
        wait_port(redis_ports[6379])
        wait_port(mq[5672])
        command(["docker", "exec", mq_name, "rabbitmq-plugins", "enable", "rabbitmq_management"])
        wait_port(mq[15672])
        manifest["infrastructure"] = {
            "isolated_run": suffix,
            "ports": {
                "postgres": pg[5432],
                "redis": redis_ports[6379],
                "rabbitmq": mq[5672],
                "rabbitmq_management": mq[15672],
            },
            "images": [
                json.loads(
                    command(["docker", "image", "inspect", image, "--format", "{{json .}} "])
                )
                for image in ("postgres:16-alpine", "redis:7-alpine", "rabbitmq:3.13-alpine")
            ],
        }
        save(output / "environment.json", manifest)
        common = ["--db-port", str(pg[5432]), "--redis-port", str(redis_ports[6379])]
        if "reads" in args.stages:
            collect("reads", "durable_read_baseline.py", [*common, "--repeats", str(args.repeats)])
        else:
            # Only a newly created disposable database is passed to this migration.
            migration_env = {
                **env,
                "AIVERO_DB_URL": f"postgresql+psycopg://postgres:baseline@127.0.0.1:{pg[5432]}/baseline",
            }
            with (output / "migration.log").open("w") as stream:
                subprocess.run(
                    [sys.executable, "-m", "alembic", "upgrade", "head"],
                    cwd=ROOT,
                    env=migration_env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=60,
                )
        if "sse" in args.stages:
            collect("sse", "durable_sse_baseline.py", common)
        if "chain" in args.stages:
            collect(
                "chain",
                "durable_chain_baseline.py",
                [*common, "--broker-port", str(mq[5672]), "--management-port", str(mq[15672])],
            )
        manifest["status"] = (
            "completed_with_measured_errors"
            if any(stage["exit_code"] for stage in stages)
            else "completed"
        )
    except BaseException as error:
        manifest["status"] = "failed"
        manifest["error"] = str(error)
        raise
    finally:
        cleanup = []
        for name in reversed(containers):
            with (output / f"{name}.log").open("w") as stream:
                subprocess.run(
                    ["docker", "logs", name],
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=30,
                    check=False,
                )
            result = subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            cleanup.append({"container": name, "removed": result.returncode == 0})
        manifest["cleanup"] = cleanup
        manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["load_average_after"] = os.getloadavg()
        source_after = fingerprints()
        manifest["backend_source_changed_during_measurement"] = [
            path
            for path in sorted(source_before.keys() | source_after.keys())
            if source_before.get(path) != source_after.get(path)
        ]
        manifest["collector_changed_during_measurement"] = [
            name
            for name, digest in manifest["collector_sha256"].items()
            if hashlib.sha256((ROOT / "scripts/perf" / name).read_bytes()).hexdigest() != digest
        ]
        manifest["artifact_sha256"] = {
            str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.rglob("*"))
            if path.is_file() and path.name != "environment.json"
        }
        save(output / "environment.json", manifest)


if __name__ == "__main__":
    main()
