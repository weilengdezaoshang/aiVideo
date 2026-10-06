"""运行指标(补充要求 §十三):内存注册表 + Outbox/任务 gauge;禁止高基数标签。"""

import pytest

from backend import metrics


def setup_function(_):
    metrics.reset()


def test_counter_and_gauge_roundtrip():
    metrics.incr("http_requests_total", {"method": "GET", "status": "200"}, 2)
    metrics.incr("http_requests_total", {"method": "GET", "status": "200"})
    metrics.incr("http_errors_total", {"method": "GET", "status": "500"})
    metrics.set_gauge("outbox_pending", 7)

    rendered = metrics.render()
    assert rendered["counters"]["http_requests_total|method=GET,status=200"] == 3
    assert rendered["counters"]["http_errors_total|method=GET,status=500"] == 1
    assert rendered["gauges"]["outbox_pending"] == 7


def test_high_cardinality_labels_rejected():
    """jobId/attemptId 等高基数字段不得作为指标标签(§十三)。"""
    with pytest.raises(ValueError):
        metrics.incr("jobs_total", {"jobId": "abc"})
    with pytest.raises(ValueError):
        metrics.set_gauge("queue_depth", 1, {"attemptId": "x"})


def test_metrics_snapshot_is_json_serializable():
    import json

    metrics.incr("a_total", {"x": "1"})
    metrics.set_gauge("g", 1.5)
    payload = json.dumps(metrics.render(), ensure_ascii=False)
    assert "a_total" in payload


def test_collect_outbox_and_job_gauges_from_database():
    """收集器从数据库聚合:Outbox 待发送数量与最老年龄、任务未知/排队数量(§十三)。"""
    import asyncio
    import uuid
    from datetime import datetime, timedelta, timezone

    from backend.infrastructure.database import DatabaseSettings
    from backend.infrastructure.uow import AsyncUnitOfWork

    import os

    TEST_DB_URL = os.environ.get("AIVERO_METRICS_TEST_DB")
    if not TEST_DB_URL:
        pytest.skip("Set AIVERO_METRICS_TEST_DB to an isolated test database")

    async def seed():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            workspace_id = uuid.uuid4()
            await uow.workspaces.ensure(workspace_id)
            job = await uow.jobs.create(workspace_id=workspace_id, kind="image", params={})
            event = await uow.outbox.enqueue(
                aggregate_type="job",
                aggregate_id=str(job.id),
                event_type="job.created",
                payload={"jobId": str(job.id)},
            )
            await uow.commit()
            # 拨老:最老事件年龄可测
            from sqlalchemy import update

            from backend.infrastructure.orm import Outbox

            await uow.session.execute(
                update(Outbox)
                .where(Outbox.id == event.id)
                .values(created_at=datetime.now(timezone.utc) - timedelta(seconds=42))
            )
            await uow.commit()

    asyncio.run(seed())
    asyncio.run(seed())
    metrics.collect_outbox_gauges(DatabaseSettings(url=TEST_DB_URL))
    metrics.collect_job_gauges(DatabaseSettings(url=TEST_DB_URL))

    rendered = metrics.render()
    assert rendered["gauges"]["outbox_pending_total"] >= 2
    assert rendered["gauges"]["outbox_oldest_age_s"] >= 42
    assert rendered["gauges"]["jobs_queued"] >= 2
    assert rendered["gauges"]["jobs_unknown"] >= 0
