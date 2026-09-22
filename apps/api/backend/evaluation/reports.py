"""指标、统计与完整状态报告(技术方案 §12)。

分母纪律(§12.1):预算中止、未执行、裁判错误不作为"内容零分"塞进条件质量平均,
但必须出现在覆盖缺口;分母为 0 显示 N/A(null),绝不产生假 0。
缓存/回放不算独立试验;mock 与 stub 的成绩只能作为流程证据并强制标注(§1.3)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Template

from ..common import now
from .models import CaseVersion, RunState, Trial, canonical_hash

STATUS_LABEL = {
    "pending": "未执行",
    "running": "执行中",
    "completed": "已完成",
    "failed": "执行失败",
    "cancelled": "已取消",
    "timed_out": "超时",
    "blocked": "被阻断",
    "unknown_external": "上游结果未知",
}

VERDICT_LABEL = {
    "pass": "质量通过",
    "fail": "质量不通过",
    "undetermined": "质量未定",
    "not_graded": "未评分",
}


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def build_report(state: RunState, cases: list[CaseVersion]) -> dict:
    manifest = state.manifest
    by_case = {case.caseId: case for case in cases}
    denominators = {
        "plan": len(state.trials),
        "attempted": 0,
        "generated": 0,
        "determined": 0,
        "pass": 0,
    }
    execution_counts: dict[str, int] = {}
    verdict_counts = {"pass": 0, "fail": 0, "undetermined": 0, "not_graded": 0}
    judge_error_trials = 0
    first_attempt_success = 0
    latencies: list[int] = []
    entries = []
    coverage_gaps: list[str] = []
    for trial in state.trials:
        execution_counts[trial.status] = execution_counts.get(trial.status, 0) + 1
        if trial.status != "pending":
            denominators["attempted"] += 1
        if trial.status == "completed" and trial.artifactIds:
            denominators["generated"] += 1
            if trial.qualityVerdict == "pass" and not _used_retry(state, trial):
                first_attempt_success += 1
        if trial.qualityVerdict is not None:
            verdict_counts[trial.qualityVerdict] += 1
            if trial.qualityVerdict == "pass":
                denominators["pass"] += 1
            if trial.qualityVerdict == "undetermined" and trial.status == "completed":
                judge_error_trials += 1
            if trial.qualityVerdict in {"pass", "fail"}:
                denominators["determined"] += 1
        elif trial.status == "completed":
            verdict_counts["not_graded"] += 1
        if trial.latencyMs is not None and trial.status == "completed":
            latencies.append(trial.latencyMs)
        check_summary = _check_summary(trial, state, by_case.get(trial.caseId))
        entries.append(
            {
                "trialId": trial.trialId,
                "caseId": trial.caseId,
                "repetitionIndex": trial.repetitionIndex,
                "status": trial.status,
                "statusLabel": STATUS_LABEL.get(trial.status, trial.status),
                "qualityVerdict": trial.qualityVerdict,
                "verdictLabel": VERDICT_LABEL.get(trial.qualityVerdict, "未评分"),
                "weightedScore": trial.weightedScore,
                "requestedSeed": trial.requestedSeed,
                "resolvedSeed": trial.resolvedSeed,
                "artifactIds": trial.artifactIds,
                "latencyMs": trial.latencyMs,
                "error": trial.error,
                "errorCategory": trial.errorCategory,
                "checks": check_summary,
            }
        )
        if trial.status != "completed":
            coverage_gaps.append(
                f"{trial.caseId}(重复 {trial.repetitionIndex}):{STATUS_LABEL.get(trial.status)}—"
                + (trial.error or "未产生可评分素材")
            )
        elif trial.qualityVerdict in {None, "undetermined"}:
            reason = "裁判失败" if any(
                grade.get("status") == "error"
                for grade in _trial_grades(state, trial.trialId)
            ) else "裁判未给出确定结论"
            coverage_gaps.append(f"{trial.caseId}(重复 {trial.repetitionIndex}):质量未定({reason})")
    metrics = {
        # 指标口径见 §12.1;null 在渲染层显示为 N/A。
        "executionCoverage": _ratio(denominators["attempted"], denominators["plan"]),
        "generationSuccessRate": _ratio(denominators["generated"], denominators["attempted"]),
        "determinationCoverage": _ratio(denominators["determined"], denominators["generated"]),
        "conditionalQualityPassRate": _ratio(denominators["pass"], denominators["determined"]),
        "planCompletionRate": _ratio(denominators["pass"], denominators["plan"]),
        "firstAttemptSuccessRate": _ratio(first_attempt_success, denominators["attempted"]),
        "costPerQualifiedArtifact": None,  # mock 无费用;live 由预算账本提供,未知保持 null
    }
    annotations = []
    if manifest.provider == "mock":
        annotations.append("mock 生成:分数仅验证管线,不代表模型质量(§1.3)")
    if manifest.grading.get("judge") == "stub":
        annotations.append("stub 判分:合成回答,仅验证判分管线")
    if manifest.legacy:
        annotations.append("legacy 导入:原记录缺失执行与裁判细节,分数不可复现(§19.2)")
    if judge_error_trials:
        annotations.append(f"{judge_error_trials} 条 trial 裁判失败,计入覆盖缺口而非内容零分")
    video_without_frames = [
        trial
        for trial in state.trials
        if trial.caseId in {case.caseId for case in cases if case.taskType in {"text_to_video", "image_to_video"}}
        and not trial.frameArtifactIds
    ]
    if video_without_frames:
        annotations.append(
            f"{len(video_without_frames)} 条视频 trial 无帧级证据(环境无 ffmpeg 或抽帧失败),"
            "时序质量指标不可得,不冒充可得(§7)"
        )
    return {
        "schemaVersion": 2,
        "reportVersion": None,  # 落盘时填报告版本号
        "runId": manifest.runId,
        "generatedAt": now(),
        "purpose": manifest.purpose,
        "mode": manifest.mode,
        "provider": manifest.provider,
        "dataset": manifest.dataset,
        "planHash": manifest.planHash,
        "denominators": denominators,
        "metrics": metrics,
        "executionCounts": execution_counts,
        "verdictCounts": verdict_counts,
        "latencyMs": {
            "count": len(latencies),
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
        },
        "usage": _usage_summary(state),
        "annotations": annotations,
        "coverageGaps": coverage_gaps,
        "entries": entries,
    }


def _used_retry(state: RunState, trial: Trial) -> bool:
    generation_successes = [
        item
        for item in state.attempts
        if item.trialId == trial.trialId
        and item.callSite == "generation"
        and item.status == "succeeded"
    ]
    return len(generation_successes) > 1


def _trial_grades(state: RunState, trial_id: str) -> list[dict]:
    return [grade.model_dump() for grade in state.grades if grade.trialId == trial_id]


def _check_summary(trial: Trial, state: RunState, case: CaseVersion | None) -> list[dict]:
    grades = {grade.checkId: grade for grade in state.grades if grade.trialId == trial.trialId}
    summary = []
    for check in case.checks if case else []:
        grade = grades.get(check.id)
        summary.append(
            {
                "checkId": check.id,
                "kind": check.kind,
                "question": check.question,
                "expected": check.expected,
                "observed": grade.observed if grade else None,
                "status": grade.status if grade else None,
                "required": check.required,
                "weight": check.weight,
            }
        )
    return summary


def _usage_summary(state: RunState) -> dict:
    """P0 只有 mock 生成与 stub 判分,无外部用量;live 的权威数据来自 P1 预算账本。"""
    known_tokens = 0
    unknown_calls = 0
    for attempt in state.attempts:
        if attempt.callSite == "generation":
            continue  # mock 生成不产生外部用量
        if attempt.usage is None:
            continue  # stub 等合成判分:没有外部调用,也没有未知费用
        if attempt.usage.source == "provider_reported" and attempt.usage.textTokens:
            known_tokens += attempt.usage.textTokens
        else:
            unknown_calls += 1
    return {
        "textTokens": known_tokens or None,
        "unknownCalls": unknown_calls or None,
        "note": "未知用量保持 null 不冒充 0(§13.1);权威账本在 P1 接入",
    }


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(q * len(ordered) + 0.999) - 1))
    return ordered[index]


_MARKDOWN_TEMPLATE = Template(
    """\
# 评测报告(完整状态口径)

- run:`{{ report.runId }}`(mode={{ report.mode }},provider={{ report.provider }})
- 数据集:`{{ report.dataset.datasetId }}@{{ report.dataset.version }}`,快照哈希 `{{ report.dataset.contentHash[:12] }}…`
- 生成时间:{{ report.generatedAt }}
{% for note in report.annotations %}- ⚠ {{ note }}
{% endfor %}
## 分母与覆盖率

- 计划 trial(N_plan):{{ report.denominators.plan }}
- 已开始(N_attempted):{{ report.denominators.attempted }};有素材(N_generated):{{ report.denominators.generated }};已判定(N_determined):{{ report.denominators.determined }};质量通过(N_pass):{{ report.denominators.pass }}

| 指标 | 数值 |
| --- | --- |
{% for name, value in metric_rows %}| {{ name }} | {{ value }} |
{% endfor %}
## 执行与质量状态

- 执行:{{ execution_summary }}
- 质量:{{ verdict_summary }}
{% if report.coverageGaps %}
## 覆盖缺口(不计入质量分母,但阻断隐含的通过率)
{% for gap in report.coverageGaps %}- {{ gap }}
{% endfor %}{% endif %}
## 逐 trial 明细

| 用例 | 重复 | 执行 | 质量 | 加权分 | seed(请求/实际) |
| --- | --- | --- | --- | --- | --- |
{% for entry in report.entries %}| {{ entry.caseId }} | {{ entry.repetitionIndex }} | {{ entry.statusLabel }} | {{ entry.verdictLabel }} | {{ entry.scoreText }} | {{ entry.seedText }} |
{% endfor %}"""
)


def render_markdown(report: dict) -> str:
    """Markdown 渲染:数值整形在 Python 完成保持口径单一,排版交给 Jinja2 模板。"""

    def fmt(value) -> str:
        return "N/A" if value is None else f"{value * 100:.1f}%"

    m = report["metrics"]
    metric_rows = [
        ("执行覆盖率 N_attempted/N_plan", fmt(m["executionCoverage"])),
        ("生成成功率 N_generated/N_attempted", fmt(m["generationSuccessRate"])),
        ("判定覆盖率 N_determined/N_generated", fmt(m["determinationCoverage"])),
        ("条件质量通过率 N_pass/N_determined", fmt(m["conditionalQualityPassRate"])),
        ("计划合格完成率 N_pass/N_plan", fmt(m["planCompletionRate"])),
        ("首次生成合格率", fmt(m["firstAttemptSuccessRate"])),
        (
            "每份合格素材成本",
            "N/A(mock 无外部费用)" if m["costPerQualifiedArtifact"] is None else "-",
        ),
    ]
    execution_summary = "、".join(
        f"{STATUS_LABEL.get(k, k)} {v}" for k, v in sorted(report["executionCounts"].items())
    )
    verdict_summary = "、".join(
        f"{VERDICT_LABEL.get(k, k)} {v}" for k, v in report["verdictCounts"].items()
    )
    entries = []
    for entry in report["entries"]:
        row = dict(entry)
        row["scoreText"] = (
            "N/A(未评)" if entry["weightedScore"] is None else f"{entry['weightedScore']:.2f}"
        )
        row["seedText"] = (
            f"{entry['requestedSeed']}/"
            f"{entry['resolvedSeed'] if entry['resolvedSeed'] is not None else '?'}"
        )
        entries.append(row)
    return _MARKDOWN_TEMPLATE.render(
        report=report,
        metric_rows=metric_rows,
        execution_summary=execution_summary,
        verdict_summary=verdict_summary,
        entries=entries,
    )


@dataclass
class ReportWriter:
    """报告不可变(§11.4/§5.4):每次生成新版本,旧报告不改写。"""

    store: Path

    def write(self, report: dict) -> tuple[Path, Path]:
        run_dir = self.store / "runs" / report["runId"] / "reports"
        version = 1 + sum(1 for _ in run_dir.iterdir()) if run_dir.is_dir() else 1
        report["reportVersion"] = version
        target = run_dir / str(version)
        target.mkdir(parents=True)
        report_file = target / "report.json"
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown = target / "report.md"
        markdown.write_text(render_markdown(report), encoding="utf-8")
        return report_file, markdown


def report_hash(report: dict) -> str:
    return canonical_hash({k: v for k, v in report.items() if k != "reportVersion"})
