"""Planner route listing returns AgentRoute.public fields only."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agent.config import AgentRoute
from backend.evaluation.api import build_eval_router


def _client(tmp_path):
    app = FastAPI()
    app.include_router(build_eval_router(tmp_path))
    return TestClient(app)


def _route() -> AgentRoute:
    return AgentRoute(
        id="eval-route",
        label="评测路由",
        provider="deepseek",
        model="deepseek-chat",
        baseUrl="https://planner-route.invalid/v1",
        credentialRef="secret-ref-not-for-api",
    )


def test_planner_routes_are_public_fields_only(tmp_path, monkeypatch):
    def refuse_credentials():
        raise AssertionError("route API 不能读取凭据")

    monkeypatch.setattr("backend.core.supervisor.wiring.deployment_routes", lambda: [_route()])
    monkeypatch.setattr("backend.core.supervisor.wiring.load_credential_map", refuse_credentials)
    response = _client(tmp_path).get("/api/evals/v1/planner/routes")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "routes": [
            {
                "id": "eval-route",
                "label": "评测路由",
                "provider": "deepseek",
                "model": "deepseek-chat",
            }
        ]
    }
    text = response.text
    assert "planner-route.invalid" not in text
    assert "secret-ref-not-for-api" not in text
    assert "apiKey" not in text
    assert "credentialRef" not in text
    assert "baseUrl" not in text


def test_planner_routes_empty_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.core.supervisor.wiring.deployment_routes", lambda: [])
    response = _client(tmp_path).get("/api/evals/v1/planner/routes")
    assert response.status_code == 200
    assert response.json() == {"routes": []}


def test_planner_routes_invalid_config_is_safe(tmp_path, monkeypatch):
    def bad_routes():
        raise ValueError("Invalid Agent route configuration https://secret.example apiKey=sk-live")

    monkeypatch.setattr("backend.core.supervisor.wiring.deployment_routes", bad_routes)
    response = _client(tmp_path).get("/api/evals/v1/planner/routes")
    assert response.status_code == 422
    assert response.json()["detail"] == "部署路由配置非法，已拒绝列出"
    assert "sk-live" not in response.text
    assert "secret.example" not in response.text
