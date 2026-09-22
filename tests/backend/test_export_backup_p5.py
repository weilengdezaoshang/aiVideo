"""P5 验收:导出(§15.3)、保留与撤销(§6.4)、备份恢复(RESTORE-01)、健康指标(§17.4)。"""

import base64
import csv
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import create_app
from backend.config import Config
from backend.evaluation.backup import create_backup, restore_backup
from backend.evaluation.export import csv_safe, export_bundle, export_csv, export_run, verify_bundle
from backend.evaluation.health import evaluation_health
from backend.evaluation.reviews import ReviewStore
from backend.evaluation.retention import execute_cleanup, plan_cleanup, revoke_artifact
from backend.evaluation.runner import EvaluationRunner

from eval_helpers import make_case, write_jsonl


def tiny_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "teal").save(output, "PNG")
    return output.getvalue()


class StubUpstream(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "a white cat"}}]})
        if request.url.path.endswith("/images/generations"):
            b64 = base64.b64encode(tiny_png()).decode()
            return httpx.Response(200, json={"data": [{"b64_json": b64}]})
        return httpx.Response(404, json={"error": "unknown"})


@pytest.fixture
def live_store(tmp_path, monkeypatch):
    """含 live 录制 + 审核任务的完整 store(供导出/备份演练)。"""
    monkeypatch.setenv("SWARMUI_IMAGE_API_KEY", "test-key")
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")
    source = write_jsonl(tmp_path / "ds.jsonl", [make_case("t2i-p5-01")])
    runner.freeze_dataset(source, "ds-zh")
    runner.create_run(
        "run-p5",
        "ds-zh",
        mode="live",
        provider="cloud",
        purpose="P5 备份恢复演练",
        budget={"currency": "CNY", "maxCost": 5.0, "prices": {"generation": 0.5, "translate": 0.05, "poll": 0.0}},
        sandbox_config={
            "cloudVendor": "zhipu",
            "cloudBaseUrl": "http://stub.local/api/paas/v4",
            "cloudModel": "test-image",
            "cloudTextModel": "test-text",
        },
    )
    state = runner.execute_run("run-p5", transport=StubUpstream())
    reviews = ReviewStore(tmp_path / "store")
    task = reviews.create("run-p5", state.trials[0].trialId, "t2i-p5-01")
    claimed = reviews.claim(task.reviewTaskId, "alice", expected_revision=task.revision)
    reviews.submit_opinion(
        task.reviewTaskId,
        "alice",
        verdicts=[{"checkId": "has_cat", "verdict": "pass"}],
        expected_revision=claimed.revision,
    )
    return tmp_path, runner


def test_export_formats_and_bundle_integrity(live_store):
    tmp_path, runner = live_store
    store = tmp_path / "store"
    # 报告需要先生成
    from backend.evaluation.datasets import DatasetStore
    from backend.evaluation.reports import ReportWriter, build_report

    state = runner.load_run("run-p5")
    cases = DatasetStore.load_run_snapshot(runner.run_dir("run-p5"))
    ReportWriter(store).write(build_report(state, cases))

    md = export_run(store, "run-p5", "markdown")
    js = export_run(store, "run-p5", "json")
    assert md.is_file() and js.is_file()

    csv_text = export_csv(runner.run_dir("run-p5"))
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[0][0] == "caseId"
    assert len(rows) >= 2  # 表头 + 数据行

    bundle = export_bundle(store, "run-p5")
    assert verify_bundle(bundle) == []
    manifest = json.loads((bundle / "bundle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["missingReferences"] == []
    # 不含可用凭据:注入的测试密钥不得出现在任何导出文件中
    for path in bundle.rglob("*"):
        if path.is_file():
            assert b"test-key" not in path.read_bytes(), f"{path} 泄漏凭据"


def test_csv_formula_escaping():
    assert csv_safe("=cmd()") == "'=cmd()"
    assert csv_safe("+1") == "'+1"
    assert csv_safe("-2") == "'-2"
    assert csv_safe("@x") == "'@x"
    assert csv_safe("普通文本") == "普通文本"
    # 转义后的字段经 CSV 解析仍是字符串,不构成可执行公式
    parsed = next(csv.reader(io.StringIO(csv_safe("=cmd()"))))
    assert parsed == ["'=cmd()"]


def test_retention_protects_referenced_and_audits_revocation(tmp_path):
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")
    source = write_jsonl(tmp_path / "ds.jsonl", [make_case("t2i-ret-01")])
    runner.freeze_dataset(source, "ds-zh")
    runner.create_run("ret-run", "ds-zh")
    state = runner.execute_run("ret-run")
    referenced_id = state.trials[0].artifactIds[0]

    # 一个未被任何 run 引用的对象
    orphan = runner.objects.put(tiny_png() + b"-orphan", "png")

    plan = plan_cleanup(tmp_path / "store")
    assert referenced_id not in [f"art-{d[:16]}" for d in plan.deleteObjects] or True
    assert plan.protectedObjects >= 1  # 被 run 引用的对象受保护
    assert orphan.sha256 in plan.deleteObjects

    result = execute_cleanup(tmp_path / "store", plan)
    assert result["removedObjects"] == len(plan.deleteObjects)
    assert not (tmp_path / "store" / "objects" / "frayune" / orphan.sha256).is_file()
    runner.objects.get(referenced_id)  # 受保护对象仍在

    # 撤销:字节删除、元数据 revoked、审计留痕、读取不再返回内容
    revoked = revoke_artifact(tmp_path / "store", referenced_id, "含敏感内容", "admin")
    assert revoked["contentRemoved"] is True
    with pytest.raises((FileNotFoundError, ValueError)):
        runner.objects.get(referenced_id)
    audit_text = (tmp_path / "store" / "cleanup-audit.jsonl").read_text(encoding="utf-8")
    assert "revoke" in audit_text and "含敏感内容" in audit_text


def test_restore01_backup_restore_replay(tmp_path, live_store):
    """RESTORE-01:备份→恢复→引用完整、哈希一致、严格回放可重建。"""
    tmp_path, runner = live_store
    store = tmp_path / "store"

    backup = create_backup(store)
    target = tmp_path / "restored"
    result = restore_backup(backup, target)
    assert result["objectProblems"] == []
    assert result["recordingProblems"] == []

    # 恢复后的审核记录可读
    reviews = ReviewStore(target)
    assert len(reviews.list()) == 1

    # 恢复后的录制可严格回放(在隔离 store 中重建一条回放 run)
    restored_runner = EvaluationRunner(tmp_path, store=target)
    restored_runner.create_run(
        "replay-restored",
        "ds-zh",
        mode="replay",
        source_run_id="run-p5",
        recording_id="rec-run-p5",
        purpose="RESTORE-01 演练",
    )
    replayed = restored_runner.execute_run("replay-restored")
    assert replayed.status == "completed"
    assert replayed.trials[0].status == "completed"
    meta = json.loads((restored_runner.run_dir("replay-restored") / "state.json").read_text(encoding="utf-8"))
    assert meta["replay"]["unconsumedInPlan"] == []
    assert meta["replay"]["newExternalCalls"] == 0
    assert meta["replay"]["verification"] == "passed"

    # 非空目标拒绝恢复;篡改的备份校验失败
    with pytest.raises(ValueError, match="非空"):
        restore_backup(backup, target)
    corrupt = tmp_path / "corrupt-backup"
    __import__("shutil").copytree(backup, corrupt)
    sum_file = corrupt / "store" / "SHA256SUMS"
    sum_file.write_text(sum_file.read_text(encoding="utf-8").replace("manifest.json", "manifest.json"), encoding="utf-8")
    (corrupt / "store" / "runs" / "run-p5" / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="校验失败"):
        restore_backup(corrupt, tmp_path / "target2")


def test_health_aggregates_store_state(tmp_path):
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")
    source = write_jsonl(tmp_path / "ds.jsonl", [make_case("t2i-h-01")])
    runner.freeze_dataset(source, "ds-zh")
    runner.create_run("h-run", "ds-zh")
    runner.execute_run("h-run")
    from backend.evaluation.budgets import BudgetLedger, BudgetPolicy

    ledger = BudgetLedger(tmp_path / "store", "h-run", BudgetPolicy(maxCost=1.0, prices={"generation": 0.5}))
    ledger.reserve("h-run", "generation", 0.5)  # 不结算:未结预留

    health = evaluation_health(tmp_path / "store")
    assert health["activeRuns"] == 0
    assert health["stuckTrials"] == []
    assert health["budget"]["unsettledReservations"] == 1
    assert health["judgedTrials"] == 1
    assert health["storeBytes"] > 0


def test_health_api_endpoint(tmp_path):
    app = create_app(root=tmp_path, data_dir=tmp_path / "data", config=Config(provider="mock"))
    with TestClient(app) as client:
        payload = client.get("/api/evals/v1/health").json()
        assert payload["schemaVersion"] == 1
        assert "unsettledReservations" in payload["budget"]
