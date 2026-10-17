"""真实 Host 故障注入验收：正常 → 注入变红 → 还原变绿。"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# CLI 自动进入项目虚拟环境；被 pytest 导入时不替换进程。
if __name__ == "__main__" and sys.prefix == sys.base_prefix:
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        sys.exit("请先创建 .venv 并安装 requirements-dev.txt")
    os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
sys.path.insert(0, str(ROOT / "apps/api"))

from backend.evaluation.agent_flow import failure_rows, load_flow_sources  # noqa: E402
from backend.evaluation.runner import EvaluationRunner  # noqa: E402

FAULTS = {
    "f1": ("L1", "l1-complex-handoff", "route", "missing_handoff"),
    "f2": ("L3", "l3-cancel", "cancel_new_artifacts", "artifact_after_cancel"),
    "f3": ("L4", "l4-cancel-continue", "continue_has_plan", "no_plan_after_continue"),
    "f4": ("L7", "l7-bad-once", "unexpected_retry", "unexpected_retry"),
}


def verify(store: Path, fault: str | None = None):
    if (store / "runs").is_dir() and any((store / "runs").iterdir()):
        raise ValueError("证据目录已有运行，请使用新的 --store，避免覆盖验收证据")
    sources = load_flow_sources(ROOT)
    selected = [fault] if fault else list(FAULTS)
    runner = EvaluationRunner(ROOT, store=store)
    original = os.environ.pop("AGENT_EVAL_FAULT", None)
    results = []
    acceptance = []
    store.mkdir(parents=True, exist_ok=True)

    def full_pass(phase):
        os.environ.pop("AGENT_EVAL_FAULT", None)
        all_cases = [case for _, case in sources]
        source = store / f"full-{phase}.jsonl"
        source.write_text("\n".join(case.model_dump_json() for case in all_cases) + "\n", encoding="utf-8")
        runner.freeze_dataset(source, f"full-{phase}")
        run_id = f"full-{phase}"
        runner.create_run(run_id, f"full-{phase}", suite="agent_flow")
        state = runner.execute_run(run_id)
        assert len(state.trials) == len(all_cases)
        assert not failure_rows(state, all_cases)
        assert all(t.status == "completed" and t.qualityVerdict == "pass" for t in state.trials)
        rows = []
        for trial in state.trials:
            if trial.tracePath:
                assert Path(trial.tracePath).is_file()
            observation = runner.run_dir(run_id) / "trials" / trial.trialId / "agent" / "observation.json"
            assert observation.is_file()
            assert any(grade.trialId == trial.trialId and grade.source == "rules" for grade in state.grades)
            rows.append({"caseId": trial.caseId, "suite": trial.flowLayer, "verdict": "PASS", "reason": None, "tracePath": trial.tracePath, "tracePaths": trial.tracePaths, "observationPath": str(observation), "traceNote": None if trial.tracePath else "协议拒绝或队列边界探针未进入 Agent 回合，见 observationPath"})
        graders = [grade.evaluator.model_dump() if hasattr(grade.evaluator, "model_dump") else grade.evaluator for grade in state.grades]
        entry = {"phase": phase, "fault": None, "runId": run_id, "verdict": "PASS", "caseCount": len(rows), "cases": rows, "evaluators": graders}
        acceptance.append(entry)
        print(json.dumps({k: v for k, v in entry.items() if k not in {"cases", "evaluators"}}, ensure_ascii=False), flush=True)

    try:
        if fault is None:
            full_pass("before")
        for name in selected:
            suite, case_id, check_id, reason = FAULTS[name]
            cases = [case for _, case in sources if case.caseId == case_id]
            assert len(cases) == 1, f"找不到目标用例 {case_id}"
            source = store / f"{name}.jsonl"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(cases[0].model_dump_json() + "\n", encoding="utf-8")
            runner.freeze_dataset(source, name)
            for phase in ("before", "fault", "after"):
                if phase == "fault":
                    os.environ["AGENT_EVAL_FAULT"] = name
                else:
                    os.environ.pop("AGENT_EVAL_FAULT", None)
                run_id = f"{name}-{phase}"
                runner.create_run(run_id, name, suite=suite)
                state = runner.execute_run(run_id)
                rows = failure_rows(state, cases)
                if phase == "fault":
                    assert any(t.caseId == case_id and t.qualityVerdict == "fail" for t in state.trials), state.trials
                    matching = [row for row in rows if row["reason"] == check_id]
                    assert matching, f"{name} 未检出 {check_id}: {rows}"
                    assert all(row["suite"] == suite and row["caseId"] == case_id for row in rows), rows
                    assert all(row["tracePath"] and Path(row["tracePath"]).is_file() for row in rows), rows
                    diagnostic = {**matching[0], "checkId": check_id, "reason": reason}
                    trace = json.loads(Path(diagnostic["tracePath"]).read_text(encoding="utf-8"))
                    assert trace["version"] == "1"
                    results.append({"fault": name, "phase": phase, "verdict": "FAIL", **diagnostic})
                else:
                    assert not rows and all(t.qualityVerdict == "pass" for t in state.trials), rows
                    trial = state.trials[0]
                    assert trial.tracePath and Path(trial.tracePath).is_file()
                    results.append({"fault": name, "phase": phase, "verdict": "PASS", "suite": suite, "caseId": case_id, "reason": None, "tracePath": trial.tracePath, "tracePaths": trial.tracePaths})
                if phase == "fault":
                    acceptance.append({"runId": run_id, **results[-1]})
                print(json.dumps(results[-1], ensure_ascii=False), flush=True)
        if fault is None:
            full_pass("after")
            (store / "acceptance.json").write_text(json.dumps(acceptance, ensure_ascii=False, indent=2), encoding="utf-8")
        (store / "sensitivity.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        os.environ.pop("AGENT_EVAL_FAULT", None)
        if original is not None:
            os.environ["AGENT_EVAL_FAULT"] = original
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fault", choices=list(FAULTS))
    parser.add_argument("--store", type=Path)
    args = parser.parse_args()
    store = args.store or Path(tempfile.mkdtemp(prefix="aivideo-sensitivity-"))
    verify(store, args.fault)
    print(f"故障注入验收 Pass：{store / 'sensitivity.json'}")


if __name__ == "__main__":
    main()
