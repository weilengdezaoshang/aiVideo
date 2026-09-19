"""指标暴露端点(补充要求 §十三):/api/metrics/runtime 渲染进程指标并采集 DB gauge。

高基数字段不进响应(§十三.9);采集失败不拖垮端点。
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app

TEST_DB_URL = os.environ.get(
    "AIVERO_TEST_DB_URL",
    "postgresql+psycopg://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test",
)


def _pg_ok():
    import psycopg

    try:
        with psycopg.connect(TEST_DB_URL.replace("+psycopg", ""), connect_timeout=3):
            return True
    except Exception:
        return False


def test_runtime_metrics_endpoint_renders_counters_and_gauges(root):
    """/api/metrics/runtime 返回 counters/gauges 结构,可 JSON 序列化。"""
    with TestClient(create_app(root)) as client:
        response = client.get("/api/metrics/runtime")
    assert response.status_code == 200
    body = response.json()
    assert "counters" in body and "gauges" in body
    json.dumps(body, ensure_ascii=False)  # 可序列化


@pytest.mark.skipif(
    not _pg_ok(),
    reason="PostgreSQL 隔离实例不可用,采集用例未执行(不视为通过)",
)
def test_runtime_metrics_collects_outbox_gauges(root, monkeypatch):
    """端点触发数据库采集:outbox_pending_total 等 gauge 出现在响应中。"""
    monkeypatch.setenv("AIVERO_DB_URL", TEST_DB_URL)
    with TestClient(create_app(root)) as client:
        response = client.get("/api/metrics/runtime")
    body = response.json()
    assert "outbox_pending_total" in body["gauges"]
    assert "jobs_queued" in body["gauges"]


def test_runtime_metrics_excludes_internal_details_on_failure(root, monkeypatch):
    """采集失败时端点仍 200(降级),不泄漏内部异常细节。"""
    from backend import metrics as metrics_mod

    def boom(settings):
        raise RuntimeError("内部连接串细节 postgres://secret")

    monkeypatch.setattr(metrics_mod, "collect_outbox_gauges", boom)
    with TestClient(create_app(root)) as client:
        response = client.get("/api/metrics/runtime")
    assert response.status_code == 200
    assert "secret" not in response.text
