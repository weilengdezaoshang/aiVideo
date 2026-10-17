"""L7 工作台源题库与启动入口复用当前 agent_flow 平台。"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.evaluation import api as eval_api


def test_l7_visible_and_launchable(tmp_path, monkeypatch):
    class Scheduler:
        def submit(self, run_id):
            pass

        def shutdown(self, wait=False):
            pass

    monkeypatch.setattr(eval_api, "_scheduler", lambda _: Scheduler())
    app = FastAPI()
    app.include_router(eval_api.build_eval_router(tmp_path))
    with TestClient(app) as client:
        preview = client.get("/api/evals/v1/agent-flow/cases?suite=L7")
        assert preview.status_code == 200
        definitions = preview.json()["cases"]
        assert len(definitions) == 4
        assert all("L7" in case["suites"] for case in definitions)
        response = client.post("/api/evals/v1/agent-flow", json={"suite": "agent_flow", "layers": ["L7"]})
        assert response.status_code == 202
        cases = client.get(f"/api/evals/v1/runs/{response.json()['runId']}/cases").json()["cases"]
        assert {case["caseId"] for case in cases} == {case["caseId"] for case in definitions}
