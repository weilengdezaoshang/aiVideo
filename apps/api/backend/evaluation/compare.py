"""版本比较(技术方案 §12.3/§12.4,验收 METRIC-02)。

- 案例按 caseId+caseVersion+repetitionIndex 配对;数据集快照不一致时拒绝
  总体均值比较,只生成"公共用例交集报告"并显式列出缺口。
- 未定项、无样本、零合格素材显示 null/N/A,不产生假 0(§12.1)。
- 单次重复不给出显著性结论:样本量与不确定性以"证据不足"表述(§12.4)。
"""

from __future__ import annotations

from .models import RunState
from .reports import build_report


def _subject_delta(base_manifest, cand_manifest) -> list[str]:
    """被测对象差异的具体描述:模型与厂商(平台)的变化点,标注 A/B 对比的对象。"""
    deltas = []
    for key in ("cloudVendor", "cloudModel", "videoModel"):
        base_value = (base_manifest.sandboxConfig or {}).get(key)
        cand_value = (cand_manifest.sandboxConfig or {}).get(key)
        if base_value != cand_value:
            deltas.append(f"{key}: {base_value or '默认'} → {cand_value or '默认'}")
    if base_manifest.model != cand_manifest.model:
        deltas.append(f"model: {base_manifest.model or '默认'} → {cand_manifest.model or '默认'}")
    if base_manifest.provider != cand_manifest.provider:
        deltas.append(f"provider: {base_manifest.provider} → {cand_manifest.provider}")
    return deltas


def build_comparison(baseline: RunState, candidate: RunState, baseline_cases=(), candidate_cases=()) -> dict:
    base_pairs = {(t.caseId, t.caseVersion, t.repetitionIndex): t for t in baseline.trials}
    cand_pairs = {(t.caseId, t.caseVersion, t.repetitionIndex): t for t in candidate.trials}
    keys_base, keys_cand = set(base_pairs), set(cand_pairs)
    paired_keys = sorted(keys_base & keys_cand)

    baseline_snapshot = baseline.manifest.dataset.get("contentHash")
    candidate_snapshot = candidate.manifest.dataset.get("contentHash")
    same_snapshot = baseline_snapshot == candidate_snapshot
    # 因子分组:证据链因子必须一致(同一把尺子);被测对象因子是实验的自变量——
    # 模型/平台不同正是 A/B 对比的目的,不应拒绝总体结论,只需显式标注(§12.3)。
    evidence_factors = {
        "grading": (baseline.manifest.grading, candidate.manifest.grading),
        "mode": (baseline.manifest.mode, candidate.manifest.mode),
    }
    subject_factors = {
        "provider": (baseline.manifest.provider, candidate.manifest.provider),
        "model": (baseline.manifest.model, candidate.manifest.model),
        "sandboxConfig": (baseline.manifest.sandboxConfig, candidate.manifest.sandboxConfig),
    }
    evidence_mismatched = [name for name, (b, c) in evidence_factors.items() if b != c]
    subject_mismatched = [name for name, (b, c) in subject_factors.items() if b != c]
    # 被测对象级差异具体到模型/厂商,便于标注"A/B 对比的是什么"
    subject_delta = _subject_delta(baseline.manifest, candidate.manifest)

    paired = []
    regressions, improvements = [], []
    check_regressions = []
    worse_statuses = {"fail", "dependency_failed"}  # 通过→失败类才算检查项退化;→未定单独呈现
    for key in paired_keys:
        base, cand = base_pairs[key], cand_pairs[key]
        base_score = base.weightedScore
        cand_score = cand.weightedScore
        delta = (
            round(cand_score - base_score, 4)
            if base_score is not None and cand_score is not None
            else None
        )
        paired.append(
            {
                "caseId": key[0],
                "caseVersion": key[1],
                "repetitionIndex": key[2],
                "baseline": {"status": base.status, "verdict": base.qualityVerdict, "score": base_score},
                "candidate": {"status": cand.status, "verdict": cand.qualityVerdict, "score": cand_score},
                "delta": delta,
            }
        )
        if delta is not None:
            if delta < 0:
                regressions.append({"caseId": key[0], "delta": delta})
            elif delta > 0:
                improvements.append({"caseId": key[0], "delta": delta})
        # 检查项级退化定位(§12.3):同检查项从通过退为失败/依赖失败,关联两侧 gradeId 可回查。
        base_grades = {g.checkId: g for g in baseline.grades if g.trialId == base.trialId}
        cand_grades = {g.checkId: g for g in candidate.grades if g.trialId == cand.trialId}
        for check_id in sorted(set(base_grades) & set(cand_grades)):
            base_grade, cand_grade = base_grades[check_id], cand_grades[check_id]
            if base_grade.status == "pass" and cand_grade.status in worse_statuses:
                check_regressions.append(
                    {
                        "caseId": key[0],
                        "checkId": check_id,
                        "baselineStatus": base_grade.status,
                        "candidateStatus": cand_grade.status,
                        "baselineGradeId": base_grade.gradeId,
                        "candidateGradeId": cand_grade.gradeId,
                        "candidateTrialId": cand.trialId,
                    }
                )

    repetitions = max(
        (len({t.repetitionIndex for t in candidate.trials if t.caseId == case}) for case in {t.caseId for t in candidate.trials}),
        default=0,
    )
    # Mock、缓存命中与回放不构成独立统计样本(§12.1):非双 live 不给总体差值。
    independent_samples = baseline.manifest.mode == "live" and candidate.manifest.mode == "live"
    if same_snapshot and not evidence_mismatched and not subject_mismatched:
        comparability = "compatible"
        note = "数据集快照与执行/评分配置一致,可进行配对比较"
    elif subject_mismatched and same_snapshot and not evidence_mismatched:
        # 被测对象不同但证据链一致:模型/平台 A/B 对比是合法且核心的用例。
        comparability = "subject_comparison"
        note = f"被测对象对比({','.join(subject_delta)}):证据链一致,总体差值成立;结论是模型/平台间对比而非同模型回归(§12.3)"
    elif paired_keys:
        comparability = "intersection_only"
        note = "数据集快照或评分/执行配置不一致:拒绝总体均值比较,以下仅为公共用例交集(§12.3)"
        if evidence_mismatched:
            note += f";差异项:{','.join(evidence_mismatched)}"
    else:
        comparability = "incompatible"
        note = "数据集快照不一致且无公共用例,无法比较"

    base_report = build_report(baseline, list(baseline_cases))
    cand_report = build_report(candidate, list(candidate_cases))
    # 错误类别聚合:候选 run 执行失败 trial 按类别分组并关联 trialId,支持按错误类别定位退化(§12.3)。
    error_categories: dict[str, list[str]] = {}
    for trial in candidate.trials:
        if trial.status == "failed":
            category = trial.errorCategory or "unknown"
            error_categories.setdefault(category, []).append(trial.trialId)
    return {
        "schemaVersion": 2,
        "baselineRunId": baseline.manifest.runId,
        "candidateRunId": candidate.manifest.runId,
        "baselinePlanHash": baseline.manifest.planHash,
        "candidatePlanHash": candidate.manifest.planHash,
        "comparability": comparability,
        "note": note,
        "datasetSnapshots": {"baseline": baseline_snapshot, "candidate": candidate_snapshot},
        "configDifferences": evidence_mismatched,
        "subjectDeltas": subject_delta,
        "independentSamples": independent_samples,
        "checkRegressions": check_regressions,
        "candidateErrorCategories": [
            {"category": category, "trialIds": trial_ids[:20]}
            for category, trial_ids in sorted(error_categories.items())
        ],
        "coverage": {
            "pairedCount": len(paired_keys),
            "onlyInBaseline": sorted(f"{k[0]}@v{k[1]}r{k[2]}" for k in keys_base - keys_cand),
            "onlyInCandidate": sorted(f"{k[0]}@v{k[1]}r{k[2]}" for k in keys_cand - keys_base),
        },
        "summary": {
            "baselinePassRate": base_report["metrics"]["conditionalQualityPassRate"],
            "candidatePassRate": cand_report["metrics"]["conditionalQualityPassRate"],
            "overallDelta": (
                None
                if comparability != "compatible" or not independent_samples or base_report["metrics"]["conditionalQualityPassRate"] is None or cand_report["metrics"]["conditionalQualityPassRate"] is None
                else round(
                    cand_report["metrics"]["conditionalQualityPassRate"]
                    - base_report["metrics"]["conditionalQualityPassRate"],
                    4,
                )
            ),
            "regressions": regressions,
            "improvements": improvements,
            "uncertainty": "证据不足:单次重复不做显著性结论(§12.4)"
            if repetitions < 2
            else f"每例 {repetitions} 次重复;差值以用例为聚类单位,置信区间见 P4 校准",
        },
        "paired": paired,
    }
