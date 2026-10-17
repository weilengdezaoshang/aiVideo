"""用例浏览读取冻结版本和运行快照，不依赖外部基础设施。"""
import shutil

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.evaluation.api import build_eval_router, _runner
from eval_helpers import make_case, write_jsonl


def test_dataset_and_run_case_definitions_are_frozen(tmp_path):
    runner = _runner(tmp_path)
    source = write_jsonl(tmp_path / "cases.jsonl", [make_case("t2i-original")])
    first = runner.freeze_dataset(source, "browse-dataset")
    runner.create_run("browse-run", "browse-dataset")
    source = write_jsonl(tmp_path / "cases.jsonl", [make_case("t2i-new")])
    runner.freeze_dataset(source, "browse-dataset")
    app = FastAPI()
    app.include_router(build_eval_router(tmp_path))
    with TestClient(app) as client:
        response = client.get(f"/api/evals/v1/datasets/browse-dataset/cases?version={first['version']}")
        assert response.status_code == 200
        assert response.json()["cases"][0]["caseId"] == "t2i-original"
        assert response.json()["cases"][0]["checks"][0]["question"]
        assert client.get("/api/evals/v1/datasets/browse-dataset/cases").json()["cases"][0]["caseId"] == "t2i-new"
        shutil.rmtree(tmp_path / "evaluation" / "manifests")
        snapshot = client.get("/api/evals/v1/runs/browse-run/cases")
        assert snapshot.status_code == 200
        assert snapshot.json()["cases"][0]["caseId"] == "t2i-original"
        assert client.get("/api/evals/v1/datasets/missing/cases").status_code == 404
        assert client.get("/api/evals/v1/runs/missing/cases").status_code == 404
        assert client.get("/api/evals/v1/datasets/browse-dataset/cases?version=oops").status_code == 422


def test_flow_preview_is_read_only_and_matches_selection(tmp_path):
    app = FastAPI()
    app.include_router(build_eval_router(tmp_path))
    with TestClient(app) as client:
        whole = client.get("/api/evals/v1/agent-flow/cases")
        assert whole.status_code == 200
        cases = whole.json()["cases"]
        assert cases and all(case["taskType"] == "agent_flow" for case in cases)
        selected = client.get("/api/evals/v1/agent-flow/cases?suite=L4")
        assert selected.status_code == 200
        assert selected.json()["cases"] == [case for case in cases if "L4" in case["suites"]]
        assert client.get("/api/evals/v1/agent-flow/cases?suite=unknown").status_code == 422
        assert not (tmp_path / "evaluation" / "manifests").exists()
        assert not (tmp_path / "evaluation" / "runs").exists()


def test_selected_flow_layers_freeze_only_the_requested_cases(tmp_path, monkeypatch):
    from backend.evaluation import api as eval_api
    class Scheduler:
        def submit(self, run_id):
            pass
        def shutdown(self, wait=False):
            pass
    monkeypatch.setattr(eval_api, "_scheduler", lambda _: Scheduler())
    app = FastAPI()
    app.include_router(build_eval_router(tmp_path))
    with TestClient(app) as client:
        response = client.post("/api/evals/v1/agent-flow", json={"suite": "agent_flow", "layers": ["L1", "L4"]})
        assert response.status_code == 202
        run_id = response.json()["runId"]
        cases = client.get(f"/api/evals/v1/runs/{run_id}/cases").json()["cases"]
        assert len(cases) == 7
        assert all(set(case["suites"]) & {"L1", "L4"} for case in cases)
        for invalid in ([], ["L6"], [{}], "L1"):
            assert client.post("/api/evals/v1/agent-flow", json={"layers": invalid}).status_code == 422
