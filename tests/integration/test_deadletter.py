"""死信恢复(补充要求 §九.8-11):受权限控制的 CLI,重放前检查业务状态,审计留痕。"""

import asyncio
import json
import os
import uuid

import pytest

from backend.infrastructure.database import DatabaseSettings
from backend.infrastructure.uow import AsyncUnitOfWork

TEST_DB_URL = os.environ.get(
    "AIVERO_TEST_DB_URL",
    "postgresql+psycopg://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test",
)
PG_URL = os.environ.get(
    "AIVERO_TEST_PG_URL", "postgresql://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test"
)


def _pg_available() -> bool:
    import psycopg

    try:
        with psycopg.connect(PG_URL, connect_timeout=3):
            return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL 隔离实例不可用,集成测试未执行(不视为通过)",
)


@pytest.fixture()
def clean_db():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        for table in (
            "outbox",
            "generation_requests",
            "jobs",
            "asset_references",
            "assets",
            "attempts",
            "documents",
        ):
            conn.exec_driver_sql(f"DELETE FROM {table}")
    engine.dispose()
    yield


async def seed_dead_event(job_status: str = "queued"):
    """建任务与一条已进入死信语义的事件(本地 status=failed 标记)。"""
    async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
        workspace_id = uuid.uuid4()
        await uow.workspaces.ensure(workspace_id)
        job = await uow.jobs.create(workspace_id=workspace_id, kind="image", params={})
        if job_status != "queued":
            await uow.jobs.transition(job.id, {"queued"}, job_status)
        event = await uow.outbox.enqueue(
            aggregate_type="job",
            aggregate_id=str(job.id),
            event_type="job.created",
            payload={"jobId": str(job.id)},
        )
        event.status = "failed"  # 死信语义:多次重试后放弃
        await uow.session.flush()
        await uow.commit()
        return job.id, event.id


@requires_pg
def test_deadletter_list_and_requeue_with_audit(clean_db, tmp_path):
    """死信可列出;重放重置为 pending 且写审计(操作者/原因/原任务/动作);终态任务拒绝重放。"""
    from backend.tools import deadletter

    job_id, event_id = asyncio.run(seed_dead_event("queued"))
    audit_path = tmp_path / "deadletter-audit.jsonl"

    listed = deadletter.list_dead(DatabaseSettings(url=TEST_DB_URL), limit=10)
    assert any(str(row["eventId"]) == str(event_id) for row in listed)

    result = deadletter.requeue(
        DatabaseSettings(url=TEST_DB_URL),
        event_id=event_id,
        operator="ops-chen",
        reason="broker 故障期间的消费者失联,人工确认后重放",
        audit_path=audit_path,
    )
    assert result["requeued"] is True

    async def event_status():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            event = await uow.outbox.get(event_id)
            return event.status, event.attempts

    status, attempts = asyncio.run(event_status())
    assert status == "pending"
    assert attempts == 0  # 重放重置尝试计数,重投递后按正常重试策略执行

    audit = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    assert any(
        row["operator"] == "ops-chen"
        and row["action"] == "requeue"
        and row["eventId"] == str(event_id)
        and row["jobId"] == str(job_id)
        and row["reason"]
        for row in audit
    )

    # 终态任务的事件不得重放(§九.9:重放前检查当前业务状态)
    job_done, event_done = asyncio.run(seed_dead_event("completed"))
    denied = deadletter.requeue(
        DatabaseSettings(url=TEST_DB_URL),
        event_id=event_done,
        operator="ops-chen",
        reason="批量恢复尝试",
        audit_path=audit_path,
    )
    assert denied["requeued"] is False
    assert denied["reason"] == "job-terminal"
    audit = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    assert any(row["action"] == "requeue-denied" and row["jobId"] == str(job_done) for row in audit)


