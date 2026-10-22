"""Owned disposable containers only; never infer a developer/production database."""

import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import create_engine, text
from kombu import Connection
from redis import Redis


def docker(*args):
    result = subprocess.run(["docker", *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


@contextmanager
def infrastructure(timeout=60):
    suffix = uuid.uuid4().hex[:12]
    names = []
    try:

        def start(kind, image, port, env):
            name = "aivideo-reliability-" + kind + "-" + suffix
            docker(
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "aivideo.reliability=" + suffix,
                "-p",
                "127.0.0.1::" + str(port),
                *[v for k, value in env.items() for v in ("-e", k + "=" + value)],
                image,
            )
            names.append(name)
            return int(docker("port", name, str(port) + "/tcp").rsplit(":", 1)[1])

        pg = start(
            "pg",
            "postgres:16-alpine",
            5432,
            {"POSTGRES_DB": "reliability_test", "POSTGRES_PASSWORD": suffix},
        )
        mq = start(
            "mq",
            "rabbitmq:3.13-alpine",
            5672,
            {"RABBITMQ_DEFAULT_USER": "test", "RABBITMQ_DEFAULT_PASS": suffix},
        )
        redis = start("redis", "redis:7-alpine", 6379, {})
        urls = {
            "AIVERO_RELIABILITY_DB": f"postgresql+psycopg://postgres:{suffix}@127.0.0.1:{pg}/reliability_test",
            "AIVERO_RELIABILITY_BROKER": f"amqp://test:{suffix}@127.0.0.1:{mq}//",
            "AIVERO_RELIABILITY_REDIS": f"redis://127.0.0.1:{redis}/0",
        }
        deadline = time.monotonic() + timeout
        while True:
            try:
                engine = create_engine(
                    urls["AIVERO_RELIABILITY_DB"], connect_args={"connect_timeout": 1}
                )
                try:
                    with engine.connect() as db:
                        db.execute(text("SELECT 1"))
                finally:
                    engine.dispose()
                with Connection(urls["AIVERO_RELIABILITY_BROKER"], connect_timeout=1) as conn:
                    conn.connect()
                client = Redis.from_url(urls["AIVERO_RELIABILITY_REDIS"], socket_connect_timeout=1)
                try:
                    client.ping()
                finally:
                    client.close()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "isolated reliability infrastructure did not become ready"
                    ) from None
                time.sleep(0.2)
        migration = subprocess.run(
            [str(Path(".venv/bin/alembic")), "upgrade", "head"],
            env={
                **os.environ,
                "AIVERO_DB_URL": urls["AIVERO_RELIABILITY_DB"],
                "PYTHONPATH": "apps/api",
            },
            capture_output=True,
            text=True,
        )
        if migration.returncode:
            raise RuntimeError("isolated migration failed: " + migration.stderr)
        yield urls
    finally:
        for name in reversed(names):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
