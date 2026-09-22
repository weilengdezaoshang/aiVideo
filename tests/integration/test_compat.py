"""兼容性(补充要求 §十二):未识别 schemaVersion 不得当正常业务执行(§十二.4);
识别版本正常处理;旧消息(部署前入队)由新 Worker 处理(§十二.3)。
"""

import os
import socket

import pytest


TEST_DB_URL = os.environ.get(
    "AIVERO_TEST_DB_URL",
    "postgresql+psycopg://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test",
)
PG_URL = TEST_DB_URL.replace("+psycopg", "")
BROKER_HOST = os.environ.get("AIVERO_TEST_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("AIVERO_TEST_BROKER_PORT", "5673"))
BROKER_URL = f"amqp://aivideo:aivideo-test@{BROKER_HOST}:{BROKER_PORT}//"


def _pg_ok():
    import psycopg

    try:
        with psycopg.connect(PG_URL, connect_timeout=3):
            return True
    except Exception:
        return False


def _broker_ok():
    try:
        with socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=2):
            return True
    except OSError:
        return False


requires = pytest.mark.skipif(
    not (_pg_ok() and _broker_ok()),
    reason="PG/RabbitMQ 隔离实例不可用,集成测试未执行(不视为通过)",
)


@pytest.fixture()
def clean_and_topology():
    from sqlalchemy import create_engine

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        for table in ("outbox", "generation_requests", "jobs", "assets"):
            conn.exec_driver_sql(f"DELETE FROM {table}")
    engine.dispose()

    from kombu import Connection

    from backend.infrastructure.messaging import (
        DEAD_QUEUE,
        GENERATION_QUEUE,
        declare_topology,
    )

    with Connection(BROKER_URL) as conn:
        declare_topology(conn)
        channel = conn.channel()
        for name in (GENERATION_QUEUE.name, DEAD_QUEUE.name):
            channel.queue_purge(name)
    from backend.workers.probe import PROBE_RESULTS

    PROBE_RESULTS.clear()
    yield {"dead_queue": DEAD_QUEUE}


@requires
def test_unknown_schema_version_is_dead_lettered_not_executed(clean_and_topology):
    """未识别 schemaVersion 的事件:不执行业务,进入死信(§十二.4)。"""
    from kombu import Connection

    from backend.infrastructure.messaging import (
        DEAD_QUEUE,
        declare_topology,
        drain_raw,
        publish_event,
    )
    from backend.workers.consumer import drain_generation_events

    executed = []

    def on_message(body, message):
        executed.append(body)
        message.ack()

    with Connection(BROKER_URL) as conn:
        declare_topology(conn)
        publish_event(
            conn,
            event_id="compat-1",
            event_type="job.created",
            schema_version="99",  # 未来版本:新 Worker 未识别
            aggregate_type="job",
            aggregate_id="j-future",
            payload={"jobId": "j-future"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        drained = drain_generation_events(conn, on_message, limit=1, timeout_s=8)
        assert drained == 0, "未识别版本不得当正常业务执行"
        assert executed == []

        dead = []
        drain_raw(conn, lambda b: dead.append(b), limit=1, timeout_s=8, queue=DEAD_QUEUE)
    assert dead and dead[0]["schemaVersion"] == "99"


@requires
def test_current_schema_version_executes_normally(clean_and_topology):
    """识别版本(当前 schemaVersion=1)正常执行;消息兼容部署前入队场景(§十二.3)。"""
    from kombu import Connection

    from backend.infrastructure.messaging import declare_topology, publish_event
    from backend.workers.consumer import drain_generation_events

    executed = []

    def on_message(body, message):
        executed.append(body)
        message.ack()

    with Connection(BROKER_URL) as conn:
        declare_topology(conn)
        publish_event(
            conn,
            event_id="compat-2",
            event_type="job.created",
            schema_version="1",
            aggregate_type="job",
            aggregate_id="j-1",
            payload={"jobId": "j-1"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        drained = drain_generation_events(conn, on_message, limit=1, timeout_s=8)
    assert drained == 1
    assert executed[0]["payload"]["jobId"] == "j-1"
