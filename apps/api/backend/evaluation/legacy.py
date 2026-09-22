"""旧评测数据导入(技术方案 §19.2)。

纪律:
- 原 run.json/scores.json/report.md 保持只读,不修改、不删除。
- v2 记录标记 legacy=true;旧记录缺失的代码、裁判修订、usage、原始响应填 null,
  并生成 completeness 清单,绝不从当前配置倒推。
- 旧 run 未保存用例快照时,标注"原用例版本无法确认",不保证可复现旧分数。
- 旧 Mock/stub 只作为流程证据;旧素材进入 objects 库后可另建 grading run 重新评分。
- 旧记录没有完整 HTTP 交互,不能升级为严格回放(P1)。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..common import now
from .artifacts import ObjectStore
from .models import EVALUATOR_LEGACY, Grade, GradeStatus, RunManifest, Trial, trial_identity
from .runner import EventLog


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class LegacyImporter:
    def __init__(self, store: Path):
        self.store = store
        self.objects = ObjectStore(store, project="frayune")

    def import_run(self, old_dir: Path, run_id: str | None = None) -> dict:
        old_dir = Path(old_dir)
        run_data = _read_json(old_dir / "run.json")
        if not isinstance(run_data, dict) or not run_data.get("runId"):
            raise ValueError(f"不是有效的旧版 run 目录(缺少 run.json):{old_dir}")
        scores = _read_json(old_dir / "scores.json")
        new_id = run_id or f"legacy-{run_data['runId']}"
        target = self.store / "runs" / new_id
        if target.exists():
            raise ValueError(f"目标 runId 已存在:{new_id}")
        target.mkdir(parents=True)
        entries = run_data.get("entries") or []
        legacy_provider = str(run_data.get("provider", "")) or str(run_data.get("providerRequested", ""))
        manifest = RunManifest(
            runId=new_id,
            createdAt=str(run_data.get("createdAt") or now()),
            purpose=f"legacy 导入自 {old_dir.name}",
            mode="mock" if legacy_provider == "mock" else "live",
            provider=legacy_provider or "unknown",
            dataset={
                "datasetId": "legacy-golden-set",
                "version": 0,
                "contentHash": "unavailable",
                "frozenAt": None,
            },
            plan=[],
            grading={
                "judge": str((scores or {}).get("judge", "unknown")),
                "rubricVersion": "unknown",
            },
            legacy=True,
            code={"commit": None, "dirty": None},
        )
        (target / "manifest.json").write_text(
            json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        trials, grades = [], []
        score_by_entry = {
            item.get("id"): item for item in ((scores or {}).get("entries") or []) if item.get("id")
        }
        completeness = self._completeness(run_data, scores)
        for entry in entries:
            case_id = str(entry.get("id", "unknown"))
            trial = Trial(
                trialId=trial_identity(new_id, case_id, 0, 0),
                runId=new_id,
                caseId=case_id,
                caseVersion=0,
                repetitionIndex=0,
                status="completed" if entry.get("status") == "completed" else "failed",
                error=entry.get("error"),
                errorCategory=entry.get("status") if entry.get("status") != "completed" else None,
                resolvedSeed=entry.get("seed"),
                requestedSeed=entry.get("seed") if entry.get("seed") is not None else -1,
                latencyMs=entry.get("elapsedMs"),
                startedAt=None,
                finishedAt=None,
            )
            artifact_ref = entry.get("artifact")
            if artifact_ref:
                artifact_file = old_dir / artifact_ref
                if artifact_file.is_file():
                    record = self.objects.put(
                        artifact_file.read_bytes(),
                        artifact_file.suffix.lstrip("."),
                        origin_run_id=new_id,
                    )
                    trial.artifactIds = [record.artifactId]
                else:
                    completeness.append(f"{case_id}:旧素材文件缺失({artifact_ref})")
            trial_ids_grades = []
            score_entry = score_by_entry.get(case_id) or {}
            for index, question in enumerate(score_entry.get("questions") or []):
                grade = self._legacy_grade(trial.trialId, case_id, index, question)
                grades.append(grade)
                trial_ids_grades.append(grade)
            if trial.status == "completed" and not trial_ids_grades:
                completeness.append(f"{case_id}:已完成但无评分记录(未评分,不计入质量分母)")
            trials.append(trial)
        self._write_trials(target, trials, grades)
        (target / "completeness.json").write_text(
            json.dumps(
                {"importedAt": now(), "sourceDir": str(old_dir), "gaps": completeness},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (target / "state.json").write_text(
            json.dumps({"status": "completed", "importedAt": now()}, ensure_ascii=False),
            encoding="utf-8",
        )
        EventLog(target).append(
            "run.imported", new_id, source=str(old_dir), trials=len(trials), gaps=len(completeness)
        )
        return {
            "runId": new_id,
            "trials": len(trials),
            "grades": len(grades),
            "completenessGaps": completeness,
        }

    @staticmethod
    def _legacy_grade(trial_id: str, case_id: str, index: int, question: dict) -> Grade:
        """旧评分仅按分数转存为 legacy 证据:evaluator 版本未知,不宣称可复现。"""
        score = question.get("score")
        status: GradeStatus = "pass" if score == 1.0 else "fail" if score == 0.0 else "inconclusive"
        key = hashlib.sha256(f"{case_id}|{index}|{question.get('q', '')}".encode("utf-8")).hexdigest()[:12]
        return Grade(
            gradeId=f"{trial_id}:legacy:{key}",
            trialId=trial_id,
            checkId=f"legacy-q{index}-{key[:6]}",
            evaluator=dict(EVALUATOR_LEGACY),
            status=status,
            observed=None,
            expected=None,
            score=score if isinstance(score, (int, float)) else None,
            evidence=[{"note": f"旧判分问题:{question.get('q', '')};旧回答:{question.get('answer', '')}"}],
            source="legacy",
        )

    @staticmethod
    def _completeness(run_data: dict, scores: dict | None) -> list[str]:
        gaps = ["代码 commit 未记录(填 null)", "裁判模型修订与完整请求未记录(填 null)",
                "usage/费用未记录(填 null,不冒充 0)", "原始 HTTP 交互未保存:不能升级为严格回放"]
        if not run_data.get("goldenSet") or not Path(str(run_data.get("goldenSet"))).is_file():
            gaps.append("原用例快照缺失:旧分数不可复现")
        else:
            gaps.append(f"原用例文件仍指向源路径({run_data['goldenSet']}):当前内容无法证明与当时一致,旧分数不可复现")
        if scores is None:
            gaps.append("无 scores.json:旧评分缺失")
        return gaps

    @staticmethod
    def _write_trials(target: Path, trials: list[Trial], grades: list[Grade]) -> None:
        trials_dir = target / "trials"
        trials_dir.mkdir()
        for trial in trials:
            directory = trials_dir / trial.trialId
            directory.mkdir()
            (directory / "trial.json").write_text(
                json.dumps(trial.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        trial_grades: dict[str, list[dict]] = {}
        for grade in grades:
            trial_grades.setdefault(grade.trialId, []).append(grade.model_dump())
        for trial in trials:
            directory = trials_dir / trial.trialId
            items = trial_grades.get(trial.trialId, [])
            if items:
                (directory / "grades.json").write_text(
                    json.dumps(
                        {"judge": "legacy", "judgedAt": None, "verdict": None,
                         "weightedScore": None, "grades": items},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
