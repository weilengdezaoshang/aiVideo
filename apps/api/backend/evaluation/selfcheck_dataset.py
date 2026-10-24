"""Import frozen self-check rows into CaseVersion. Labels stay inside params for grading."""

from __future__ import annotations

from pathlib import Path

from backend.evaluation.models import CaseInput, CaseVersion, Check, Provenance
from backend.evaluation.selfcheck_cases import FIXTURE_PATH, SelfCheckCase, load_cases

DATASET_ID = "plan-selfcheck"


def selfcheck_checks(case: SelfCheckCase) -> list[Check]:
    return [
        Check(
            id="should_reject",
            kind="boolean",
            question="漂移案例应拒绝，正常案例应放行",
            expected=not case.label_pass,
        ),
        Check(
            id="legal_type_pass",
            kind="boolean",
            question="审核器的合法性与类型门通过",
            expected=True,
        ),
    ]


def case_version_from_selfcheck(case: SelfCheckCase) -> CaseVersion:
    return CaseVersion(
        caseId=case.case_id,
        language="zh-CN",
        taskType="plan_selfcheck",
        tags=["drift" if case.drift else "normal", case.kind, "plan_selfcheck"],
        suites=["plan_selfcheck", "regression"],
        title=case.case_id,
        input=CaseInput(
            prompt=case.user_message,
            params={"kind": "plan_selfcheck", "selfcheckCase": case.model_dump(mode="json")},
        ),
        checks=selfcheck_checks(case),
        provenance=Provenance(source="synthetic", note="tests/fixtures/plan-selfcheck-cases.jsonl"),
    )


def load_selfcheck_case_versions(path: Path | None = None) -> list[CaseVersion]:
    return [case_version_from_selfcheck(case) for case in load_cases(path or FIXTURE_PATH)]


def write_selfcheck_dataset(path: Path, source: Path | None = None) -> Path:
    versions = load_selfcheck_case_versions(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(item.model_dump_json() + "\n" for item in versions), encoding="utf-8")
    return path


def embedded_selfcheck_case(case: CaseVersion) -> SelfCheckCase:
    raw = case.input.params.get("selfcheckCase")
    if not isinstance(raw, dict):
        raise ValueError(f"{case.caseId}:缺少 selfcheckCase")
    return SelfCheckCase.model_validate(raw)
