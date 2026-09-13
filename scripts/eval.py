"""生成质量评测管线:run(固定参数批量生成) → judge(判分) → report(对比报告)。

用法(.venv/bin/python):
  scripts/eval.py run   --set evals/golden-set.jsonl --provider mock   # 批量生成,产物落 run 目录
  scripts/eval.py judge <run-dir> [--judge stub|qwen-vl]               # 对产物逐题判分
  scripts/eval.py report <run-dir> [baseline-dir]                      # 单次摘要或两次对比

边界:provider=mock 的分数仅用于验证管线,不代表模型质量;报告会强制标注。
真实评测需 --provider cloud(费用只发生在生成与 qwen-vl 判分调用,预算与 traces 同源记录)。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
# 运行 `python scripts/eval.py` 时 sys.path[0] 是 scripts/,其中的 backend.py 会遮蔽
# 替换 scripts 路径并加入 apps/api，保证 backend 包不会被同名启动脚本遮蔽。
sys.path[0] = str(ROOT)
sys.path.insert(0, str(ROOT / "apps/api"))
DEFAULT_OUT = ROOT / "data" / "evals"
JOB_TIMEOUT_SEC = 300
STUB_ANSWER = "(stub 判分,仅验证管线)"


def read_jsonl(path: Path) -> list[dict]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def score_answer(answer: str, expects: list[str]) -> float:
    text = answer.strip().strip("。.!")
    for expect in expects:
        if expect in {"是", "否"}:
            if text.startswith(expect):
                return 1.0
        elif expect.lower() in text.lower():
            return 1.0
    return 0.0


def entry_score(questions: list[dict]) -> float:
    total_weight = sum(q.get("weight", 1) for q in questions)
    if not total_weight:
        return 0.0
    return sum(q.get("score", 0.0) * q.get("weight", 1) for q in questions) / total_weight


def load_run(run_dir: Path) -> tuple[dict, dict | None]:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    scores_path = run_dir / "scores.json"
    scores = json.loads(scores_path.read_text(encoding="utf-8")) if scores_path.exists() else None
    return run, scores


def cmd_run(args: argparse.Namespace) -> int:
    from backend.app import create_app, ROOT as APP_ROOT

    entries = read_jsonl(Path(args.set))
    if args.only:
        entries = [e for e in entries if args.only in e["id"] or args.only in e["bucket"]]
    if not entries:
        print(f"golden set 中没有匹配的条目:{args.only or '(全部)'}")
        return 1

    run_dir = Path(args.out) / (args.run_id or time.strftime("%Y%m%d-%H%M%S"))
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    previous_provider = os.environ.get("SWARMUI_PROVIDER")
    os.environ["SWARMUI_PROVIDER"] = args.provider
    app = create_app(APP_ROOT, data_dir=run_dir / "data")
    run = dict(
        runId=run_dir.name,
        createdAt=now_iso(),
        goldenSet=str(Path(args.set)),
        providerRequested=args.provider,
        entries=[],
    )
    try:
        with TestClient(app) as client:
            config = client.get("/api/config").json()["config"]
            run["provider"] = config["provider"]
            run["model"] = config.get("cloudModel") or ""
            models = client.get("/api/models").json().get("models", [])
            default_model = models[0]["id"] if models else ""
            for entry in entries:
                record = dict(
                    id=entry["id"],
                    bucket=entry["bucket"],
                    prompt=entry["prompt"],
                    params=entry["params"],
                    status="pending",
                )
                run["entries"].append(record)
                started = time.monotonic()
                response = client.post(
                    "/api/generate",
                    json={**entry["params"], "prompt": entry["prompt"], "model": default_model},
                )
                if response.status_code != 202:
                    record.update(status="submit_failed", error=response.json().get("error", ""))
                    continue
                job_id = response.json()["jobId"]
                record["jobId"] = job_id
                deadline = time.monotonic() + JOB_TIMEOUT_SEC
                while time.monotonic() < deadline:
                    job = client.get(f"/api/jobs/{job_id}").json()["job"]
                    if job["status"] in {"completed", "failed"}:
                        break
                    time.sleep(0.05)
                record["elapsedMs"] = int((time.monotonic() - started) * 1000)
                if job["status"] != "completed" or not job.get("images"):
                    record.update(status=job["status"], error=job.get("error", ""))
                    continue
                image = job["images"][0]
                artifact = run_dir / "artifacts" / f"{entry['id']}.{image['file'].rsplit('.', 1)[-1]}"
                artifact.write_bytes(client.get(image["url"]).content)
                record.update(
                    status="completed",
                    artifact=str(artifact.relative_to(run_dir)),
                    seed=image["params"].get("seed"),
                )
    finally:
        if previous_provider is None:
            os.environ.pop("SWARMUI_PROVIDER", None)
        else:
            os.environ["SWARMUI_PROVIDER"] = previous_provider

    (run_dir / "run.json").write_text(
        json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    completed = sum(1 for e in run["entries"] if e["status"] == "completed")
    print(f"run {run_dir.name}: {completed}/{len(run['entries'])} 完成 → {run_dir}")
    return 0 if completed == len(run["entries"]) else 1


def judge_question_qwen_vl(image_path: Path, question: str, settings: dict) -> str:
    """qwen-vl 判分:兼容模式多模态消息,温度 0,只回答问题不解释。"""
    import httpx

    data_url = "data:image/png;base64," + base64.b64encode(image_path.read_bytes()).decode()
    response = httpx.post(
        settings["base_url"].rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {settings['api_key']}"},
        json={
            "model": settings["model"],
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": f"只回答问题,不要解释:{question}"},
                    ],
                }
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def cmd_judge(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    run, _ = load_run(run_dir)
    judge_name = args.judge
    settings = {}
    if judge_name == "qwen-vl":
        settings = dict(
            base_url=os.environ.get("EVAL_JUDGE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            api_key=os.environ.get("EVAL_JUDGE_API_KEY") or os.environ.get("SWARMUI_IMAGE_API_KEY", ""),
            model=os.environ.get("EVAL_JUDGE_MODEL", "qwen-vl-max-latest"),
        )
        if not settings["api_key"]:
            print("缺少判分 API Key:请设置 EVAL_JUDGE_API_KEY 或 SWARMUI_IMAGE_API_KEY")
            return 1

    results = []
    for entry in run["entries"]:
        if entry["status"] != "completed":
            results.append(dict(id=entry["id"], status=entry["status"], score=None, questions=[]))
            continue
        questions = []
        golden = next((e for e in read_jsonl(Path(run["goldenSet"])) if e["id"] == entry["id"]), None)
        for spec in (golden or {}).get("questions", []):
            if judge_name == "stub":
                answer, score = STUB_ANSWER, 1.0
            else:
                answer = judge_question_qwen_vl(run_dir / entry["artifact"], spec["q"], settings)
                score = score_answer(answer, spec["expect"])
            questions.append(
                dict(q=spec["q"], expect=spec["expect"], answer=answer, score=score,
                     weight=spec.get("weight", 1))
            )
        results.append(
            dict(id=entry["id"], bucket=entry["bucket"], status="completed",
                 score=entry_score(questions), questions=questions)
        )

    buckets: dict[str, dict] = {}
    for item in results:
        if item["score"] is None:
            continue
        stat = buckets.setdefault(item["bucket"], {"score": 0.0, "count": 0})
        stat["score"] += item["score"]
        stat["count"] += 1
    for stat in buckets.values():
        stat["score"] = round(stat["score"] / stat["count"], 4)
    scores = dict(judge=judge_name, judgedAt=now_iso(), provider=run["provider"],
                  buckets=buckets, entries=results)
    (run_dir / "scores.json").write_text(
        json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"judge({judge_name}):", json.dumps(buckets, ensure_ascii=False))
    return 0


def format_row(cells: list[str], widths: list[int]) -> str:
    return "| " + " | ".join(cell.ljust(w) for cell, w in zip(cells, widths)) + " |"


def cmd_report(args: argparse.Namespace) -> int:
    run_b, scores_b = load_run(Path(args.run_dir))
    run_a = scores_a = None
    if args.baseline_dir:
        run_a, scores_a = load_run(Path(args.baseline_dir))
    lines = ["# 生成质量评测报告", ""]
    for label, run in [("对比 run", run_b), ("基线 run", run_a)]:
        if run:
            lines.append(f"- {label}: {run['runId']}(provider={run['provider']}, 生成于 {run['createdAt']})")
    if scores_b:
        lines.append(f"- 判分器: {scores_b['judge']}")
    for name, run in [("对比", run_b), ("基线", run_a)]:
        if run and run["provider"] == "mock":
            lines.append(f"- ⚠ {name}为 mock 运行:分数仅验证管线,不代表模型质量。")
    lines.append("")

    by_bucket: dict[str, dict] = {}
    for tag, scores in (("b", scores_b), ("a", scores_a)):
        if not scores:
            continue
        grouped: dict[str, list[float]] = {}
        for item in scores["entries"]:
            if item["score"] is None:
                continue
            grouped.setdefault(item["bucket"], []).append(item["score"])
        for bucket, values in grouped.items():
            by_bucket.setdefault(bucket, {})[tag] = sum(values) / len(values)

    widths = [18, 8, 10, 10, 10]
    lines.append(format_row(["桶", "条数", "基线", "对比", "Δ"], widths))
    lines.append(format_row(["-" * 18, "-" * 8, "-" * 10, "-" * 10, "-" * 10], widths))
    for bucket in sorted(by_bucket):
        stats = by_bucket[bucket]
        score_a, score_b = stats.get("a"), stats.get("b")
        delta = "" if score_a is None or score_b is None else f"{score_b - score_a:+.2f}"
        lines.append(format_row(
            [bucket[:18], str(scores_b["buckets"].get(bucket, {}).get("count", 0)),
             f"{score_a:.2f}" if score_a is not None else "-",
             f"{score_b:.2f}" if score_b is not None else "-", delta],
            widths,
        ))
    lines.append("")

    if scores_a:
        lines.append("## 逐题退化明细")
        regressions = []
        base_scores = {e["id"]: {q["q"]: q["score"] for q in e["questions"]} for e in scores_a["entries"]}
        for entry in scores_b["entries"]:
            for question in entry["questions"]:
                before = base_scores.get(entry["id"], {}).get(question["q"])
                if before is not None and question["score"] < before:
                    regressions.append(
                        f"- [{entry['bucket']}] {entry['id']}「{question['q']}」 {before:.2f} → {question['score']:.2f}"
                    )
        lines.extend(regressions or ["(无退化)"])

    report = "\n".join(lines) + "\n"
    report_path = Path(args.run_dir) / "report.md"
    report_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"报告已写入 {report_path}")
    return 0


# ---------- 新评测核心 CLI(技术方案 §12/§19.5;exit 0 成功 / 2 配置与能力错误) ----------


def _runner(store_arg):
    from backend.evaluation.runner import EvaluationRunner

    store = Path(store_arg) if store_arg else None
    return EvaluationRunner(ROOT, store=store)


def cmd_dataset_validate(args) -> int:
    from backend.evaluation.datasets import load_cases, validate_source_params

    try:
        cases = load_cases(Path(args.path))
        problems = validate_source_params(cases)
    except (OSError, ValueError) as exc:
        print(f"校验失败:{exc}")
        return 2
    if problems:
        print("预检失败:")
        for problem in problems:
            print(f"- {problem}")
        return 2
    print(f"校验通过:{len(cases)} 条用例,共 {sum(len(c.checks) for c in cases)} 个检查项")
    return 0


def cmd_dataset_freeze(args) -> int:
    try:
        manifest = _runner(args.store).freeze_dataset(
            Path(args.path), args.dataset_id, split=args.split
        )
    except (OSError, ValueError) as exc:
        print(f"冻结失败:{exc}")
        return 2
    print(
        f"已冻结 {manifest['datasetId']}@{manifest['version']} "
        f"({manifest['caseCount']} 条用例,内容哈希 {manifest['contentHash'][:12]}…)"
    )
    return 0


def cmd_runs_create(args) -> int:
    runner = _runner(args.store)
    try:
        runner.create_run(
            args.run_id,
            args.dataset,
            args.version,
            repetitions=args.repetitions,
            purpose=args.purpose,
        )
        state = runner.execute_run(args.run_id)
    except (OSError, ValueError) as exc:
        print(f"运行失败:{exc}")
        return 2
    completed = sum(1 for t in state.trials if t.status == "completed")
    print(f"run {args.run_id}: {completed}/{len(state.trials)} 完成,状态 {state.status}")
    report_file, markdown = _write_report(runner, args.run_id)
    print(f"报告:{markdown}")
    return 0 if state.status == "completed" else 1


def cmd_runs_replay(args) -> int:
    """严格回放(§19.5 eval replay --recording --strict):强制离线,不接受 live fallback。"""
    runner = _runner(args.store)
    try:
        source = runner.load_run(args.source)
        recording = args.recording or source.manifest.recordingId
        runner.create_run(
            args.run_id,
            source.manifest.dataset.get("datasetId", ""),
            mode="replay",
            source_run_id=args.source,
            recording_id=recording,
            purpose=args.purpose or f"严格回放 {args.source}",
        )
        state = runner.execute_run(args.run_id)
    except (OSError, ValueError) as exc:
        print(f"回放失败:{exc}")
        return 2
    meta = json.loads((runner.run_dir(args.run_id) / "state.json").read_text(encoding="utf-8")).get(
        "replay", {}
    )
    completed = sum(1 for t in state.trials if t.status == "completed")
    print(
        f"回放 {args.run_id}: {completed}/{len(state.trials)} 完成,"
        f"匹配交互 {meta.get('matched', 0)},未消费 {len(meta.get('unconsumed', []))},新增外部调用 0"
    )
    for trial in state.trials:
        if trial.status != "completed":
            print(f"- {trial.caseId}: {trial.status} {trial.error or ''}")
            return 1
    return 0


def _write_report(runner, run_id: str):
    from backend.evaluation.reports import ReportWriter, build_report
    from backend.evaluation.datasets import DatasetStore

    state = runner.load_run(run_id)
    cases = DatasetStore.load_run_snapshot(runner.run_dir(run_id))
    report = build_report(state, cases)
    return ReportWriter(runner.store).write(report)


def cmd_runs_report(args) -> int:
    runner = _runner(args.store)
    try:
        _, markdown = _write_report(runner, args.run_id)
    except (OSError, ValueError) as exc:
        print(f"报告生成失败:{exc}")
        return 2
    print(f"报告已写入 {markdown}")
    return 0


def cmd_runs_show(args) -> int:
    import json as _json

    runner = _runner(args.store)
    try:
        state = runner.load_run(args.run_id)
    except (OSError, ValueError) as exc:
        print(f"读取失败:{exc}")
        return 2
    summary = {
        "runId": state.manifest.runId,
        "status": state.status,
        "mode": state.manifest.mode,
        "provider": state.manifest.provider,
        "dataset": state.manifest.dataset,
        "planCount": len(state.manifest.plan),
        "purpose": state.manifest.purpose,
        "trials": [
            {
                "trialId": t.trialId,
                "caseId": t.caseId,
                "status": t.status,
                "verdict": t.qualityVerdict,
                "score": t.weightedScore,
            }
            for t in state.trials
        ],
    }
    print(_json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_runs_list(args) -> int:
    import json as _json

    print(_json.dumps(_runner(args.store).list_runs(), ensure_ascii=False, indent=2))
    return 0


def cmd_legacy_import(args) -> int:
    from backend.evaluation.legacy import LegacyImporter

    store = Path(args.store) if args.store else ROOT / "data" / "evaluation"
    try:
        result = LegacyImporter(store).import_run(Path(args.run_dir), args.run_id)
    except (OSError, ValueError) as exc:
        print(f"导入失败:{exc}")
        return 2
    print(
        f"已导入 {result['runId']}:{result['trials']} 条 trial、{result['grades']} 条 legacy 评分"
    )
    for gap in result["completenessGaps"]:
        print(f"- 缺失:{gap}")
    return 0


def cmd_export(args) -> int:
    from backend.evaluation.export import export_run

    store = Path(args.store) if args.store else ROOT / "data" / "evaluation"
    try:
        target = export_run(store, args.run_id, args.format, Path(args.out) if args.out else None)
    except (OSError, ValueError) as exc:
        print(f"导出失败:{exc}")
        return 2
    print(f"已导出 {args.format}:{target}")
    return 0


def cmd_cost(args) -> int:
    """单 run 成本效率报表(§17.4):已结算口径与每份合格素材成本;缺失显示 N/A。"""
    import json as _json

    from backend.evaluation.drift import cost_efficiency

    store = Path(args.store) if args.store else ROOT / "data" / "evaluation"
    try:
        report = cost_efficiency(store, args.run_id)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 2
    print(_json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_drift(args) -> int:
    """漂移抽样报表(§17.4):近窗口通过率对比;--create-reviews 为漂移 run 建 risk 审核任务。"""
    import json as _json

    from backend.evaluation.drift import create_drift_reviews, drift_report

    store = Path(args.store) if args.store else ROOT / "data" / "evaluation"
    try:
        report = drift_report(store, window=args.window, threshold=args.threshold)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 2
    if args.create_reviews and report["drifted"]:
        tasks = create_drift_reviews(store, report)
        report["createdReviewTasks"] = [task.reviewTaskId for task in tasks]
    print(_json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_promptfoo(args) -> int:
    """Promptfoo 只读适配入口(§3.3):导出证据视图与可直接运行的配置,不重新生成/判分。"""
    import json as _json

    from backend.evaluation.integrations import build_promptfoo_tests, write_promptfoo_config

    store = Path(args.store) if args.store else ROOT / "data" / "evaluation"
    try:
        view = build_promptfoo_tests(store, args.run_id)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 2
    out_dir = Path(args.out) if args.out else store / "promptfoo" / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    view_file = out_dir / "results.json"
    view_file.write_text(_json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8")
    config_file = write_promptfoo_config(store, args.run_id, out_dir / "promptfooconfig.yaml.json")
    print(f"已导出 Promptfoo 只读视图:{view_file}")
    print(f"已写配置:{config_file}")
    print("使用:promptfoo view / promptfoo assert —— 只展示项目内已有判定;不触发重新生成或付费评分。")
    return 0


def cmd_health(args) -> int:
    import json as _json

    from backend.evaluation.health import evaluation_health

    store = Path(args.store) if args.store else ROOT / "data" / "evaluation"
    print(_json.dumps(evaluation_health(store), ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成质量评测管线")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="批量生成评测产物(旧入口,保持兼容)")
    run.add_argument("--set", default=str(ROOT / "evals" / "golden-set.jsonl"))
    run.add_argument("--provider", default="mock", choices=["mock", "cloud", "comfyui"])
    run.add_argument("--out", default=str(DEFAULT_OUT))
    run.add_argument("--run-id", default=None)
    run.add_argument("--only", default=None, help="只跑 id/bucket 包含该子串的条目")
    run.set_defaults(func=cmd_run)

    judge = sub.add_parser("judge", help="对产物判分(旧入口,保持兼容)")
    judge.add_argument("run_dir")
    judge.add_argument("--judge", default="stub", choices=["stub", "qwen-vl"])
    judge.set_defaults(func=cmd_judge)

    report = sub.add_parser("report", help="输出摘要或两次对比报告(旧入口,保持兼容)")
    report.add_argument("run_dir")
    report.add_argument("baseline_dir", nargs="?", default=None)
    report.set_defaults(func=cmd_report)

    dataset = sub.add_parser("dataset", help="v2 用例数据集:校验与冻结(技术方案 §4)")
    dataset_sub = dataset.add_subparsers(dest="dataset_command", required=True)
    dataset_validate = dataset_sub.add_parser("validate", help="校验 v2 用例文件")
    dataset_validate.add_argument("path")
    dataset_validate.set_defaults(func=cmd_dataset_validate)
    dataset_freeze = dataset_sub.add_parser("freeze", help="冻结为不可变数据集版本")
    dataset_freeze.add_argument("path")
    dataset_freeze.add_argument("--id", dest="dataset_id", required=True)
    dataset_freeze.add_argument("--split", default="dev")
    dataset_freeze.add_argument("--store", default=None, help="评测库目录(默认 data/evaluation)")
    dataset_freeze.set_defaults(func=cmd_dataset_freeze)

    runs = sub.add_parser("runs", help="新评测核心:创建/执行/报告(§12/§14)")
    runs_sub = runs.add_subparsers(dest="runs_command", required=True)
    runs_create = runs_sub.add_parser("create", help="冻结计划并执行一次 mock 评测")
    runs_create.add_argument("--dataset", required=True, help="datasetId")
    runs_create.add_argument("--version", type=int, default=None, help="默认最新冻结版本")
    runs_create.add_argument("--run-id", required=True)
    runs_create.add_argument("--repetitions", type=int, default=1)
    runs_create.add_argument("--purpose", default="")
    runs_create.add_argument("--store", default=None)
    runs_create.set_defaults(func=cmd_runs_create)
    runs_replay = runs_sub.add_parser("replay", help="严格回放源 run 的录制(强制离线)")
    runs_replay.add_argument("--source", required=True, help="被回放的 runId")
    runs_replay.add_argument("--run-id", required=True)
    runs_replay.add_argument("--recording", default=None, help="默认使用源 run 的录制")
    runs_replay.add_argument("--purpose", default="")
    runs_replay.add_argument("--store", default=None)
    runs_replay.set_defaults(func=cmd_runs_replay)
    runs_report = runs_sub.add_parser("report", help="生成新版本完整状态报告")
    runs_report.add_argument("run_id")
    runs_report.add_argument("--store", default=None)
    runs_report.set_defaults(func=cmd_runs_report)
    runs_show = runs_sub.add_parser("show", help="查看 run 状态投影")
    runs_show.add_argument("run_id")
    runs_show.add_argument("--store", default=None)
    runs_show.set_defaults(func=cmd_runs_show)
    runs_list = runs_sub.add_parser("list", help="列出全部 run")
    runs_list.add_argument("--store", default=None)
    runs_list.set_defaults(func=cmd_runs_list)

    legacy = sub.add_parser("legacy", help="旧版 run 导入(§19.2)")
    legacy_sub = legacy.add_subparsers(dest="legacy_command", required=True)
    legacy_import = legacy_sub.add_parser("import", help="导入旧 run 目录(只读原数据)")
    legacy_import.add_argument("run_dir")
    legacy_import.add_argument("--run-id", default=None, help="默认 legacy-<旧runId>")
    legacy_import.add_argument("--store", default=None)
    legacy_import.set_defaults(func=cmd_legacy_import)

    exporter = sub.add_parser("export", help="报告与证据导出(§15.3/§19.5)")
    exporter.add_argument("run_id")
    exporter.add_argument("--format", default="markdown", choices=["markdown", "json", "csv", "bundle"])
    exporter.add_argument("--out", default=None)
    exporter.add_argument("--store", default=None)
    exporter.set_defaults(func=cmd_export)

    cost = sub.add_parser("cost", help="单 run 成本效率报表(§17.4,只读)")
    cost.add_argument("run_id")
    cost.add_argument("--store", default=None)
    cost.set_defaults(func=cmd_cost)

    drift = sub.add_parser("drift", help="漂移抽样报表(§17.4,只读;可显式创建审核任务)")
    drift.add_argument("--window", type=int, default=5)
    drift.add_argument("--threshold", type=float, default=0.05)
    drift.add_argument("--create-reviews", action="store_true",
                       help="为漂移 run 的通过 trial 创建 risk 审核任务(默认只报告)")
    drift.add_argument("--store", default=None)
    drift.set_defaults(func=cmd_drift)

    promptfoo = sub.add_parser(
        "promptfoo", help="Promptfoo 只读适配:导出结果视图与配置(不重复执行/判分)"
    )
    promptfoo.add_argument("run_id")
    promptfoo.add_argument("--store", default=None)
    promptfoo.add_argument("--out", default=None, help="输出目录(默认 data/evaluation/promptfoo/<runId>)")
    promptfoo.set_defaults(func=cmd_promptfoo)

    health = sub.add_parser("health", help="运维健康指标(§17.4,只读聚合)")
    health.add_argument("--store", default=None)
    health.set_defaults(func=cmd_health)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())