"""liveness 与 readiness 分离(补充要求 §十三.1/.2/.3)。

- liveness(/api/health):进程活着即返回,不探测依赖;
- readiness(/api/health/ready):数据库不可用 → 503(拒绝新的可靠受理);
  broker 不可用 → 200 且标记 degraded(broker 不可用仍可落库受理,§十三.2)。
"""

from fastapi.testclient import TestClient

from backend.app import create_app


def test_health_endpoint_stays_liveness_only(root):
    """/api/health 不探测数据库:仅进程与 provider 状态。"""
    with TestClient(create_app(root)) as client:
        response = client.get("/api/health")
    assert response.status_code == 200


def test_readiness_reports_database_and_broker_components(root, monkeypatch):
    """/api/health/ready 分别报告 database/broker;数据库不可用 → 503。"""
    monkeypatch.setenv("AIVERO_DB_URL", "postgresql+psycopg://x:x@127.0.0.1:59998/x")
    with TestClient(create_app(root)) as client:
        response = client.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["components"]["database"] == "down"
    assert body["components"]["broker"] in {"up", "degraded"}


def test_readiness_503_body_carries_safe_contract(root, monkeypatch):
    """503 响应沿用统一错误契约(code/recovery),不携带连接串等内部细节。"""
    monkeypatch.setenv("AIVERO_DB_URL", "postgresql+psycopg://x:x@127.0.0.1:59998/x")
    with TestClient(create_app(root)) as client:
        response = client.get("/api/health/ready")
    body = response.json()
    assert body["code"] == "UPSTREAM_UNAVAILABLE"
    assert "59998" not in response.text
