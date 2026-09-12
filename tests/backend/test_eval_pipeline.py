"""评测管线端到端测试:mock 生成 + stub 判分,完全离线(npm run verify 门禁)。"""

import importlib.util
import json

from backend.app import ROOT


def load_eval_module():
    spec = importlib.util.spec_from_file_location("eval_cli", ROOT / "scripts" / "eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_judge_report_full_pipeline(tmp_path):
    eval_cli = load_eval_module()
    out = tmp_path / "evals"
    assert eval_cli.main(["run", "--provider", "mock", "--out", str(out), "--run-id", "r1"]) == 0
    run_dir = out / "r1"
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["provider"] == "mock"
    assert len(run["entries"]) == 8
    assert all(entry["status"] == "completed" for entry in run["entries"])
    for entry in run["entries"]:
        assert (run_dir / entry["artifact"]).is_file()
        assert entry["seed"] == entry["params"]["seed"]

    assert eval_cli.main(["judge", str(run_dir), "--judge", "stub"]) == 0
    scores = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))
    assert scores["judge"] == "stub"
    assert set(scores["buckets"]) == {"t2i.subject", "t2i.style", "t2i.text", "t2i.count"}
    assert all(stat["score"] == 1.0 for stat in scores["buckets"].values())
    questions = scores["entries"][0]["questions"]
    assert all(q["answer"] == eval_cli.STUB_ANSWER for q in questions)

    assert eval_cli.main(["report", str(run_dir)]) == 0
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "mock 运行" in report  # 分数无效警示必须存在
    assert "t2i.subject" in report


def test_same_seed_reproducible_across_runs(tmp_path):
    eval_cli = load_eval_module()
    out = tmp_path / "evals"
    common = ["--provider", "mock", "--out", str(out), "--only", "t2i-subject-01"]
    assert eval_cli.main(["run", *common, "--run-id", "a"]) == 0
    assert eval_cli.main(["run", *common, "--run-id", "b"]) == 0
    first = (out / "a" / "artifacts" / "t2i-subject-01.png").read_bytes()
    second = (out / "b" / "artifacts" / "t2i-subject-01.png").read_bytes()
    assert first == second  # 固定 seed 是评测可复现的前提


def test_report_compare_detects_regression(tmp_path):
    eval_cli = load_eval_module()
    out = tmp_path / "evals"
    for run_id in ("base", "cand"):
        assert eval_cli.main(["run", "--provider", "mock", "--out", str(out), "--run-id", run_id]) == 0
        assert eval_cli.main(["judge", str(out / run_id), "--judge", "stub"]) == 0
    # 人为制造一次退化:候选 run 的第一题判 0 分
    scores_path = out / "cand" / "scores.json"
    scores = json.loads(scores_path.read_text(encoding="utf-8"))
    scores["entries"][0]["questions"][0]["score"] = 0.0
    scores["entries"][0]["score"] = eval_cli.entry_score(scores["entries"][0]["questions"])
    scores["buckets"]["t2i.subject"]["score"] = 0.5
    scores_path.write_text(json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8")

    assert eval_cli.main(["report", str(out / "cand"), str(out / "base")]) == 0
    report = (out / "cand" / "report.md").read_text(encoding="utf-8")
    assert "逐题退化明细" in report
    assert "0.00" in report and "1.00" in report
    assert "-0.33" in report  # 桶级 Δ(按 entries 聚合:(1/3+1)/2 相对基线 1.0)
