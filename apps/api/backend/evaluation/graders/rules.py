"""类型化规则评估与依赖图判定(技术方案 §10.4/§10.5)。

设计要点:
- 裁判回答(checkId → typed observed)与规则判定分离;规则不做任何文本挖掘。
- 依赖图按拓扑序求有效状态:父项 fail/dependency_failed → 子项 dependency_failed;
  父项 error/inconclusive → 子项 inconclusive(无法判定,阻断用例结论)。
- 原始 observed 永不覆盖(GRADE-02:保留"没有杯子但杯子是红色"的矛盾证据)。
- 加权分数中 dependency_failed 保留在分母并贡献 0(§10.5:缺失主体不得抬高分数)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import (
    EVALUATOR_TYPED_RULES,
    Check,
    Grade,
    GradeEvidence,
    GradeSource,
    GradeStatus,
    TrialQualityVerdict,
)


@dataclass
class Answer:
    """裁判对单个检查项的结构化回答;observed=None 表示无法判定。"""

    checkId: str
    observed: bool | int | float | str | None
    evidence: str | None = None
    source: GradeSource = "rules"
    artifactId: str | None = None


def compare_typed(check: Check, observed: bool | int | float | str | None) -> bool | None:
    """类型化比较;类型不符或 observed 为 None 返回 None(不可判定),绝不做子串匹配。"""
    if observed is None:
        return None
    if check.kind == "boolean":
        return observed is True if isinstance(observed, bool) else None
    if check.kind == "integer":
        if isinstance(observed, bool) or not isinstance(observed, int):
            return None
        return observed == check.expected
    if check.kind == "number":
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            return None
        tolerance = check.tolerance if check.tolerance is not None else 0
        return abs(float(observed) - float(check.expected)) <= tolerance  # type: ignore[operator]
    if check.kind == "text":
        if not isinstance(observed, str):
            return None
        # 仅做规范化等值(strip + casefold);不包含、不近似、不挖掘(§8.3 同一纪律)。
        return observed.strip().casefold() == str(check.expected).strip().casefold()
    return None


def judge_answers_to_grades(
    trial_id: str, checks: list[Check], answers: dict[str, Answer]
) -> list[Grade]:
    """把裁判结构化回答转为初始 Grade(evaluator 临时记为 typed-rules,
    status 先按单项比较,依赖归并由 resolve_dependencies 完成)。"""
    grades = []
    for check in checks:
        answer = answers.get(check.id)
        if answer is None:
            status: GradeStatus = "error"
            observed = None
            evidence_note = "裁判未返回该检查项的回答"
        else:
            observed = answer.observed
            matched = compare_typed(check, observed)
            if matched is None:
                status = "inconclusive"
                evidence_note = "裁判回答类型与检查项不匹配或明确表示无法判定"
            else:
                status = "pass" if matched else "fail"
                evidence_note = answer.evidence
        grades.append(
            Grade(
                gradeId=f"{trial_id}:{check.id}",
                trialId=trial_id,
                checkId=check.id,
                evaluator=dict(EVALUATOR_TYPED_RULES),
                status=status,
                observed=observed,
                expected=check.expected,
                score=None if status in {"error", "inconclusive"} else (1.0 if status == "pass" else 0.0),
                evidence=[GradeEvidence(artifactId=answer.artifactId if answer else None, note=evidence_note)],
                source=answer.source if answer else "rules",
            )
        )
    return grades


def dependency_order(checks: list[Check]) -> list[Check]:
    """拓扑序(Kahn);CaseVersion 校验已保证无环,这里只决定求值顺序。"""
    by_id = {check.id: check for check in checks}
    remaining = {check.id: set(check.dependsOn) for check in checks}
    ordered: list[Check] = []
    while remaining:
        ready = [cid for cid, deps in remaining.items() if not (deps & set(remaining))]
        if not ready:  # 理论不可达:建用例时已拒绝成环
            ready = sorted(remaining)
        for cid in sorted(ready):
            ordered.append(by_id[cid])
            remaining.pop(cid)
    return ordered


def resolve_dependencies(checks: list[Check], grades: dict[str, Grade]) -> None:
    """原地按依赖图改写有效 status(原始 observed/证据保留在 Grade 上)。"""
    blocking_fail = {"fail", "dependency_failed"}
    blocking_unknown = {"error", "inconclusive"}
    for check in dependency_order(checks):
        grade = grades[check.id]
        dep_statuses = [grades[dep].status for dep in check.dependsOn]
        if any(status in blocking_fail for status in dep_statuses):
            grade.status = "dependency_failed"
            grade.score = 0.0
        elif any(status in blocking_unknown for status in dep_statuses):
            grade.status = "inconclusive"
            grade.score = None


def case_result(
    trial_id: str, checks: list[Check], grades: dict[str, Grade]
) -> tuple[TrialQualityVerdict, float | None]:
    """§10.5 汇总:必需项未定 → undetermined;必需项失败 → fail;否则 pass。
    weightedScore 只聚合可计分状态;dependency_failed 计入分母贡献 0。"""
    resolve_dependencies(checks, grades)
    required = [check for check in checks if check.required]
    statuses = [grades[check.id].status for check in required]
    if any(status in {"error", "inconclusive"} for status in statuses):
        verdict: TrialQualityVerdict = "undetermined"
    elif any(status in {"fail", "dependency_failed"} for status in statuses):
        verdict = "fail"
    else:
        verdict = "pass"
    scorable = [
        check
        for check in checks
        if grades[check.id].status in {"pass", "fail", "dependency_failed"}
    ]
    total_weight = sum(check.weight for check in scorable)
    score = (
        sum((grades[check.id].score or 0.0) * check.weight for check in scorable) / total_weight
        if total_weight
        else None
    )
    return verdict, score


@dataclass
class CaseGrading:
    verdict: TrialQualityVerdict
    score: float | None
    grades: list[Grade] = field(default_factory=list)


def grade_case(trial_id: str, checks: list[Check], answers: dict[str, Answer]) -> CaseGrading:
    """完整单用例判分:裁判回答 → 初始 Grade → 依赖归并 → 用例结论。"""
    grades = judge_answers_to_grades(trial_id, checks, answers)
    by_check = {grade.checkId: grade for grade in grades}
    verdict, score = case_result(trial_id, checks, by_check)
    return CaseGrading(verdict=verdict, score=score, grades=list(by_check.values()))
