"""版本比较(技术方案 §12.3/§12.4,验收 METRIC-02)。

- 案例按 caseId+caseVersion+repetitionIndex 配对;数据集快照不一致时拒绝
  总体均值比较,只生成"公共用例交集报告"并显式列出缺口。
- 未定项、无样本、零合格素材显示 null/N/A,不产生假 0(§12.1)。
- 单次重复不给出显著性结论:样本量与不确定性以"证据不足"表述(§12.4)。
"""

from __future__ import annotations

from .models import RunState
from .reports import build_report


def build_comparison(baseline: RunState, candidate: RunState, baseline_cases=(), candidate_cases=()) -> dict:
    base_pairs = {(t.caseId, t.caseVersion, t.repetitionIndex): t for t in baseline.trials}
    cand_pairs = {(t.caseId, t.caseVersion, t.repetitionIndex): t for t in candidate.trials}
    keys_base, keys_cand = set(base_pairs), set(cand_pairs)
    paired_keys = sorted(keys_base & keys_cand)

    baseline_snapshot = baseline.manifest.dataset.get("contentHash")
    candidate_snapshot = candidate.manifest.dataset.get("contentHash")
    same_snapshot = baseline_snapshot == candidate_snapshot
    # 可比性还要求:模型配置、评分配置与执行模式一致;不一致时降级为交集呈现。
    config_factors = {
        "provider": (baseline.manifest.provider, candidate.manifest.provider),
        "sandboxConfig": (baseline.manifest.sandboxConfig, candidate.manifest.sandboxConfig),
        "grading": (baseline.manifest.grading, candidate.manifest.grading),
        "mode": (baseline.manifest.mode, candidate.manifest.mode),
    }
    mismatched = [name for name, (b, c) in config_factors.items() if b != c]

    paired = []
    regressions, improvements = [], []
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

    repetitions = max(
        (len({t.repetitionIndex for t in candidate.trials if t.caseId == case}) for case in {t.caseId for t in candidate.trials}),
        default=0,
    )
    # Mock、缓存命中与回放不构成独立统计样本(§12.1):非双 live 不给总体差值。
    independent_samples = baseline.manifest.mode == "live" and candidate.manifest.mode == "live"
    if same_snapshot and not mismatched:
        comparability = "compatible"
        note = "数据集快照与执行/评分配置一致,可进行配对比较"
    elif paired_keys:
        comparability = "intersection_only"
        note = "数据集快照或配置不一致:拒绝总体均值比较,以下仅为公共用例交集(§12.3)"
        if mismatched:
            note += f";差异项:{','.join(mismatched)}"
    else:
        comparability = "incompatible"
        note = "数据集快照不一致且无公共用例,无法比较"

    base_report = build_report(baseline, list(baseline_cases))
    cand_report = build_report(candidate, list(candidate_cases))
    return {
        "schemaVersion": 2,
        "baselineRunId": baseline.manifest.runId,
        "candidateRunId": candidate.manifest.runId,
        "baselinePlanHash": baseline.manifest.planHash,
        "candidatePlanHash": candidate.manifest.planHash,
        "comparability": comparability,
        "note": note,
        "datasetSnapshots": {"baseline": baseline_snapshot, "candidate": candidate_snapshot},
        "configDifferences": mismatched,
        "independentSamples": independent_samples,
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
