"""Rule scoring for session references. LLM scores stay unset."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from backend.core.session_reference_intent import reference_intent
from backend.core.session_references import ReferenceQuery, ReferenceSettings, resolve
from backend.evaluation.reference_cases import ReferenceCase


class ReferenceScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    status_match: bool
    artifact_match: bool
    reason_match: bool
    observed_status: str
    observed_artifact_id: str | None
    observed_reason: str
    rule_scored: bool = True
    llm_tested: bool = False
    llm_untested_reason: str = "规则评测不调用模型；真实 LLM 成绩需独立记录，不并入本分。"


class ReferenceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_count: int
    rule_pass: int
    rule_rate: float
    llm_tested: bool = False
    llm_untested_reason: str
    by_category: dict[str, dict]


def observe_case(case: ReferenceCase, settings: ReferenceSettings | None = None) -> tuple[str, str | None, str]:
    """Parse the utterance. Caller session and batch are context, never expected fields."""

    try:
        raw = reference_intent(case.utterance, session_id=case.caller_session_id, batch_id=case.batch_id)
    except ValueError as exc:
        if str(exc) != "ambiguous_reference_intent":
            raise
        return "ambiguous", None, "ambiguous_reference_intent"
    if raw is None:
        return "unavailable", None, "no_reference_intent"
    found = resolve(case.artifacts, ReferenceQuery.model_validate(raw), settings)
    return found.status, found.artifact_id, found.reason


def score_case(case: ReferenceCase, settings: ReferenceSettings | None = None) -> ReferenceScore:
    status, artifact_id, reason = observe_case(case, settings)
    return ReferenceScore(
        case_id=case.case_id,
        status_match=status == case.expected_status,
        artifact_match=artifact_id == case.expected_artifact_id,
        reason_match=reason == case.expected_reason,
        observed_status=status,
        observed_artifact_id=artifact_id,
        observed_reason=reason,
    )


def grade_recorded(case: ReferenceCase, observation: object) -> ReferenceScore | str:
    """Compare a persisted observation with the label. Never resolves the utterance again."""

    if not isinstance(observation, dict):
        return "observation_missing_or_corrupt"
    if observation.get("expected_used_as_observation") is True:
        return "拒绝把 expected 当作观测"
    if any(key not in observation for key in ("observed_status", "observed_artifact_id", "observed_reason")):
        return "observation_missing_or_corrupt"
    status = observation["observed_status"]
    artifact_id = observation["observed_artifact_id"]
    reason = observation["observed_reason"]
    if status not in {"resolved", "ambiguous", "unavailable"} or not isinstance(reason, str) or not reason:
        return "observation_missing_or_corrupt"
    if artifact_id is not None and not isinstance(artifact_id, str):
        return "observation_missing_or_corrupt"
    return ReferenceScore(
        case_id=case.case_id,
        status_match=status == case.expected_status,
        artifact_match=artifact_id == case.expected_artifact_id,
        reason_match=reason == case.expected_reason,
        observed_status=status,
        observed_artifact_id=artifact_id,
        observed_reason=reason,
    )


def summarize(scores: list[ReferenceScore], cases: list[ReferenceCase]) -> ReferenceSummary:
    passed = sum(1 for item in scores if item.status_match and item.artifact_match and item.reason_match)
    by_id = {case.case_id: case.category for case in cases}
    buckets: dict[str, dict] = {}
    for item in scores:
        category = by_id[item.case_id]
        slot = buckets.setdefault(category, {"pass": 0, "count": 0})
        slot["count"] += 1
        if item.status_match and item.artifact_match and item.reason_match:
            slot["pass"] += 1
    reason = scores[0].llm_untested_reason if scores else ReferenceScore(
        case_id="none", status_match=False, artifact_match=False, reason_match=False,
        observed_status="unavailable", observed_artifact_id=None, observed_reason="no_artifact",
    ).llm_untested_reason
    return ReferenceSummary(
        case_count=len(scores), rule_pass=passed,
        rule_rate=(passed / len(scores)) if scores else 0.0,
        llm_untested_reason=reason, by_category=buckets,
    )
