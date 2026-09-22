"""报告与证据导出(技术方案 §15.3、§19.5 eval export)。

- markdown/json/csv 三种报告导出;CSV 对可能执行公式的用户文本做转义(§15.3)。
- bundle 证据包:manifest、数据集快照、事件账本、逐 trial 记录、报告、被引用素材、
  预算日志、关联审核与录制,附 SHA256SUMS;不包含可用凭据(录制头已脱敏)。
- 导出是只读操作:不修改 store 内任何记录。
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from pathlib import Path

from ..common import now
from .runner import EvaluationRunner

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value) -> str:
    """CSV 公式注入转义:以 =/+/-/@/制表符开头的单元格加前缀 '(§15.3)。"""
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def _runner(store: Path) -> EvaluationRunner:
    return EvaluationRunner(Path(__file__).resolve().parents[4], store=store)


def latest_report(run_dir: Path) -> tuple[int, dict] | None:
    reports_dir = run_dir / "reports"
    if not reports_dir.is_dir():
        return None
    versions = [int(p.name) for p in reports_dir.iterdir() if p.name.isdigit()]
    if not versions:
        return None
    latest = max(versions)
    report = json.loads((reports_dir / str(latest) / "report.json").read_text(encoding="utf-8"))
    return latest, report


def export_csv(run_dir: Path) -> str:
    _, report = latest_report(run_dir)
    if report is None:
        raise FileNotFoundError(f"run 没有报告,先执行 runs report:{run_dir.name}")
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["caseId", "repetitionIndex", "trialId", "status", "verdict", "score", "seedRequested", "seedResolved", "checkId", "checkStatus", "observed", "expected", "source", "error"])
    for entry in report["entries"]:
        checks = entry.get("checks") or [{}]
        for check in checks:
            writer.writerow(
                [
                    csv_safe(entry["caseId"]),
                    entry["repetitionIndex"],
                    csv_safe(entry["trialId"]),
                    csv_safe(entry["status"]),
                    csv_safe(entry["verdictLabel"]),
                    entry["weightedScore"] if entry["weightedScore"] is not None else "N/A",
                    entry["requestedSeed"],
                    entry["resolvedSeed"] if entry["resolvedSeed"] is not None else "?",
                    csv_safe(check.get("checkId", "")),
                    csv_safe(check.get("status", "")),
                    csv_safe(check.get("observed", "")),
                    csv_safe(check.get("expected", "")),
                    csv_safe(""),
                    csv_safe(entry["error"] or ""),
                ]
            )
    return buffer.getvalue()


def export_bundle(store: Path, run_id: str, out_dir: Path | None = None) -> Path:
    """证据包:引用完整、带校验和、不含凭据;只读拷贝。"""
    runner = _runner(store)
    state = runner.load_run(run_id)
    run_dir = runner.run_dir(run_id)
    target = (out_dir or (store / "exports")) / f"{run_id}-bundle"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    copied: list[str] = []
    files = []

    def copy_into(src: Path, relative: str) -> None:
        if not src.is_file():
            return
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(relative)
        files.append(relative)

    copy_into(run_dir / "manifest.json", "manifest.json")
    copy_into(run_dir / "state.json", "state.json")
    copy_into(run_dir / "events.jsonl", "events.jsonl")
    for item in ("cases.jsonl", "manifest.json"):
        copy_into(run_dir / "dataset" / item, f"dataset/{item}")
    for trial_dir in sorted((run_dir / "trials").iterdir()) if (run_dir / "trials").is_dir() else []:
        for name in ("trial.json", "grades.json", "attempts.json"):
            copy_into(trial_dir / name, f"trials/{trial_dir.name}/{name}")
    latest = latest_report(run_dir)
    if latest is not None:
        version = latest[0]
        for name in ("report.json", "report.md"):
            copy_into(run_dir / "reports" / str(version) / name, f"reports/{version}/{name}")
    for name in ("completeness.json",):
        copy_into(run_dir / name, name)
    # 被引用素材(内容寻址,拷贝即含哈希可校验)
    for artifact_id in sorted({a for t in state.trials for a in t.artifactIds}):
        try:
            record, _ = runner.objects.get(artifact_id)
        except (FileNotFoundError, ValueError):
            continue  # 缺失素材在 manifest 缺失清单中如实标注
        copy_into(store / "objects" / "frayune" / record.sha256, f"objects/{record.sha256}")
        copy_into(store / "objects" / "frayune" / f"{artifact_id}.json", f"objects/{artifact_id}.json")
    # 预算账本与关联审核、录制(录制头已在写入前脱敏)
    copy_into(store / "budgets" / f"{run_id}.jsonl", f"budgets/{run_id}.jsonl")
    recording_id = state.manifest.recordingId
    if recording_id:
        recording_dir = store / "recordings" / recording_id
        for name in ("meta.json", "interactions.jsonl"):
            copy_into(recording_dir / name, f"recordings/{recording_id}/{name}")
    for task_dir in sorted((store / "reviews").glob("review-*")) if (store / "reviews").is_dir() else []:
        task_file = task_dir / "task.json"
        if task_file.is_file():
            task = json.loads(task_file.read_text(encoding="utf-8"))
            if task.get("runId") == run_id:
                for name in ("task.json", "opinions.jsonl", "adjudications.jsonl"):
                    copy_into(task_dir / name, f"reviews/{task_dir.name}/{name}")

    # 缺失引用清单(诚实呈现,不伪装完整)
    missing = []
    for artifact_id in sorted({a for t in state.trials for a in t.artifactIds}):
        try:
            runner.objects.get(artifact_id)
        except (FileNotFoundError, ValueError) as exc:
            missing.append(str(exc))

    checksums = {}
    for relative in files:
        data = (target / relative).read_bytes()
        checksums[relative] = hashlib.sha256(data).hexdigest()
    manifest = {
        "schemaVersion": 1,
        "kind": "frayune-eval-bundle",
        "runId": run_id,
        "exportedAt": now(),
        "planHash": state.manifest.planHash,
        "datasetContentHash": state.manifest.dataset.get("contentHash"),
        "files": checksums,
        "missingReferences": missing,
        "note": "证据包为只读副本;录制请求头已脱敏,不包含可用凭据(§16)",
    }
    (target / "bundle-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sums = "\n".join(f"{digest}  {name}" for name, digest in sorted(checksums.items())) + "\n"
    (target / "SHA256SUMS").write_text(sums, encoding="utf-8")
    return target


def verify_bundle(bundle: Path) -> list[str]:
    """校验证据包:SHA256SUMS 全部一致;返回问题清单(空=通过)。"""
    sums_file = bundle / "SHA256SUMS"
    if not sums_file.is_file():
        return ["缺少 SHA256SUMS"]
    problems = []
    for line in sums_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split("  ", 1)
        path = bundle / name
        if not path.is_file():
            problems.append(f"缺少文件:{name}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            problems.append(f"哈希不一致:{name}")
    return problems


def export_run(store: Path, run_id: str, fmt: str, out_dir: Path | None = None) -> Path:
    """统一入口:markdown | json | csv | bundle(§19.5 eval export)。"""
    runner = _runner(store)
    run_dir = runner.run_dir(run_id)
    out = out_dir or (store / "exports")
    out.mkdir(parents=True, exist_ok=True)
    if fmt == "markdown":
        latest = latest_report(run_dir)
        if latest is None:
            raise FileNotFoundError(f"run 没有报告:{run_id}")
        target = out / f"{run_id}-report.md"
        shutil.copy2(run_dir / "reports" / str(latest[0]) / "report.md", target)
        return target
    if fmt == "json":
        latest = latest_report(run_dir)
        if latest is None:
            raise FileNotFoundError(f"run 没有报告:{run_id}")
        target = out / f"{run_id}-report.json"
        shutil.copy2(run_dir / "reports" / str(latest[0]) / "report.json", target)
        return target
    if fmt == "csv":
        target = out / f"{run_id}-report.csv"
        target.write_text(export_csv(run_dir), encoding="utf-8")
        return target
    if fmt == "bundle":
        return export_bundle(store, run_id, out_dir=out)
    raise ValueError(f"未知导出格式:{fmt}(支持 markdown|json|csv|bundle)")
