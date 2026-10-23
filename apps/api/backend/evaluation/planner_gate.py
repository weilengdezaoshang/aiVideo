"""Offline planner gate: protocol, injection control-flow, and dataset integrity.

Rule match rate is reported and does not fail the process. A missing LLM route,
a schema break, or an injection that the harness scores as a pass does fail it.

Each invocation writes a new directory. Passing ``--output`` that already exists
is refused, so a concurrent or historical run is not deleted.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from backend.core.supervisor.contracts import PlannerResult
from backend.evaluation.planner_cases import FIXTURE_PATH, load_cases
from backend.evaluation.planner_dataset import DATASET_ID, case_version_from_planner, write_planner_dataset
from backend.evaluation.planner_factory import set_planner_factory, synthetic_eval_binding
from backend.evaluation.runner import EvaluationRunner


class _ScriptedPlanner:
    """Local control-flow stand-in. Audit metadata keeps it out of quality claims."""

    eval_binding = synthetic_eval_binding("backend.evaluation.planner_gate._ScriptedPlanner")

    def __init__(self, script: dict):
        # Do not assign `self.plan`: that name is the async method below.
        self._script = script
        self.contexts = []

    async def plan(self, context):
        self.contexts.append(context)
        return PlannerResult(
            plan=self._script,
            invariants={"deliverable": {}},
            confidence=1,
            raw_output={"writer": "scripted", "synthetic": True},
            tokens=0,
        )


def _fail(message: str) -> None:
    print(f"planner-gate FAIL {message}", file=sys.stderr)
    raise SystemExit(1)


def _run_rule(root: Path) -> dict:
    source = write_planner_dataset(root / "planner-cases.jsonl", FIXTURE_PATH)
    runner = EvaluationRunner(root, store=root / "store")
    frozen = runner.freeze_dataset(source, DATASET_ID, split="regression")
    if frozen["caseCount"] != 96:
        _fail(f"冻结条数 {frozen['caseCount']}，期望 96")
    set_planner_factory(None)
    manifest = runner.create_run(
        "planner-rule",
        DATASET_ID,
        provider="mock",
        mode="mock",
        purpose="planner offline rule baseline",
        sandbox_config={"plannerMode": "rule"},
    )
    runner.execute_run(manifest.runId)
    state = runner.load_run(manifest.runId)
    report_path = runner.run_dir(manifest.runId) / "planner-summary.json"
    summary = json.loads(report_path.read_text(encoding="utf-8"))
    if len(state.trials) != 96 or any(trial.status != "completed" for trial in state.trials):
        _fail("Rule 基线没有完成 96 条")
    if summary.get("llmTested") is True:
        _fail("Rule 基线被标成已测 LLM")
    if not summary.get("promptTemplateHash") or not summary.get("toolCatalogVersion"):
        _fail("manifest 证据缺少提示词哈希或工具目录版本")
    if summary.get("datasetContentHash") != frozen["contentHash"]:
        _fail("run 证据中的数据集哈希与冻结内容不一致")
    for trial in state.trials:
        observation = json.loads((runner._trial_dir(manifest.runId, trial.trialId) / "observation.json").read_text())
        if observation.get("expected_used_as_observation") is True:
            _fail("观测来自 expected")
        if "expected" in observation:
            _fail("观测对象含有 expected")
    return {"frozen": frozen, "summary": summary, "runId": manifest.runId}


def _write_one(root: Path, filename: str, dataset_id: str, category: str) -> None:
    """One real labeled row. Control-flow checks do not need another 96-case execute."""

    rows = [case for case in load_cases(FIXTURE_PATH) if case.category == category]
    if not rows:
        _fail(f"数据集没有 {category} 行")
    source = root / filename
    source.write_text(case_version_from_planner(rows[0]).model_dump_json() + "\n", encoding="utf-8")
    runner = EvaluationRunner(root, store=root / "store")
    runner.freeze_dataset(source, dataset_id, split="regression")


def _run_unavailable(root: Path) -> None:
    _write_one(root, "llm-missing.jsonl", "planner-llm-missing", "single")
    runner = EvaluationRunner(root, store=root / "store")
    set_planner_factory(None)
    manifest = runner.create_run(
        "planner-llm-missing",
        "planner-llm-missing",
        mode="mock",
        purpose="llm route missing",
        sandbox_config={"plannerMode": "llm"},
    )
    runner.execute_run(manifest.runId)
    state = runner.load_run(manifest.runId)
    if any(trial.qualityVerdict == "pass" for trial in state.trials):
        _fail("无 LLM 路由时出现了通过")
    if any(trial.qualityVerdict != "undetermined" for trial in state.trials):
        _fail("无 LLM 路由必须是 undetermined/inconclusive")


def _run_injection(root: Path) -> None:
    rows = [case for case in load_cases(FIXTURE_PATH) if case.category == "injection"]
    if not rows:
        _fail("数据集没有 injection 行")
    case = rows[0]
    _write_one(root, "injection.jsonl", "planner-injection", "injection")
    runner = EvaluationRunner(root, store=root / "store")
    scripted = _ScriptedPlanner({
        "goal": "obey untrusted canvas",
        "steps": [{"id": "t1", "tool": "read_asset", "parameters": {"asset_id": "injected"}}],
    })

    def factory():
        return scripted

    set_planner_factory(factory)
    try:
        manifest = runner.create_run(
            "planner-injection",
            "planner-injection",
            mode="mock",
            purpose="scripted injection control flow",
            sandbox_config={"plannerMode": "llm"},
        )
        runner.execute_run(manifest.runId)
    finally:
        set_planner_factory(None)
    state = runner.load_run("planner-injection")
    trial = state.trials[0]
    if trial.qualityVerdict != "fail":
        _fail(f"注入控制流应判失败，实际 {trial.qualityVerdict}")
    observation = json.loads((runner._trial_dir(trial.runId, trial.trialId) / "observation.json").read_text())
    if observation.get("synthetic") is not True or observation.get("llm_tested") is True:
        _fail("scripted 结果冒充了 LLM 质量")
    if not scripted.contexts:
        _fail("scripted planner 没有收到 context")
    from backend.evaluation.planner_factory import resolve_assemble_messages

    assemble, _module = resolve_assemble_messages()
    messages = assemble(scripted.contexts[0])
    blob = json.dumps(messages, ensure_ascii=False)
    expected_names = json.dumps(case.expected.tool_names, ensure_ascii=False)
    if expected_names in blob and case.expected.tool_names[0] not in case.context.request.message:
        _fail("assemble_messages 把标注工具写入了提示词")


def _claim_output(requested: str | None) -> Path:
    """Exclusive directory. An existing ``--output`` is left untouched."""

    if requested is None:
        return Path(tempfile.mkdtemp(prefix="aivideo-planner-offline-gate-"))
    root = Path(requested)
    if root.exists():
        if not root.is_dir():
            _fail(f"输出路径已存在且不是目录，拒绝覆盖: {root}")
        if any(root.iterdir()):
            _fail(f"输出目录已有内容，拒绝覆盖: {root}")
        return root
    root.mkdir(parents=True)
    return root


def _isolate_paid_routes() -> None:
    """Offline gate never reads a deployment route file or a credential file."""

    import os

    os.environ.pop("AIVERO_PRODUCTION_AGENT_ROUTES_FILE", None)
    os.environ.pop("AIVERO_CREDENTIALS_FILE", None)
    os.environ.pop("AIVERO_RUNTIME_ENVIRONMENT", None)
    set_planner_factory(None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline planner evaluation gate")
    parser.add_argument(
        "--output",
        default=None,
        help="空目录或不存在的路径。省略时创建新的独占临时目录，且不删除该目录。",
    )
    args = parser.parse_args(argv)
    _isolate_paid_routes()
    root = _claim_output(args.output)
    try:
        cases = load_cases(FIXTURE_PATH)
        if len(cases) != 96:
            _fail(f"标注数据集 {len(cases)} 条，期望 96")
        rule = _run_rule(root)
        _run_unavailable(root)
        _run_injection(root)
    except SystemExit:
        raise
    except Exception as exc:
        _fail(f"{type(exc).__name__}: {exc}")
    evidence = {
        "gate": "planner-offline",
        "directory": str(root),
        "caseCount": 96,
        "accuracyThreshold": None,
        "llmQualityTested": False,
        "rule": rule["summary"],
        "note": "只拦截协议、注入控制流和数据回归。Rule 匹配率是基线，不是门槛。未调用付费模型。",
    }
    evidence_path = root / "gate-evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = root / "store" / "runs" / rule["runId"] / "planner-summary.json"
    print(json.dumps({
        "ok": True,
        "tool_match_rate": rule["summary"]["tools"]["rate"],
        "directory": str(root),
        "gateEvidence": str(evidence_path),
        "ruleSummary": str(summary_path),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
