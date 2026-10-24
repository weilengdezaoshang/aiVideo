"""Import reference rows into CaseVersion. Rule checks only."""
from __future__ import annotations

from pathlib import Path

from backend.evaluation.models import CaseInput, CaseVersion, Check, Provenance
from backend.evaluation.reference_cases import FIXTURE_PATH, ReferenceCase, load_cases

DATASET_ID = "session-reference"


def reference_checks() -> list[Check]:
    return [
        Check(id="status_match", kind="boolean", question="指代状态与标注一致", expected=True),
        Check(id="artifact_match", kind="boolean", question="命中产物与标注一致", expected=True),
        Check(id="reason_match", kind="boolean", question="原因码与标注一致", expected=True),
    ]


def case_version_from_reference(case: ReferenceCase) -> CaseVersion:
    return CaseVersion(
        caseId=case.case_id,
        language="zh-CN",
        taskType="session_reference",
        tags=[case.category, "session_reference"],
        suites=["session_reference", "regression"],
        title=case.utterance,
        input=CaseInput(prompt=case.utterance, params={
            "kind": "session_reference",
            "referenceCase": case.model_dump(mode="json"),
        }),
        checks=reference_checks(),
        provenance=Provenance(source="synthetic", note="tests/fixtures/session-reference-cases.jsonl"),
    )


def load_reference_case_versions(path: Path | None = None) -> list[CaseVersion]:
    return [case_version_from_reference(case) for case in load_cases(path or FIXTURE_PATH)]


def write_reference_dataset(path: Path, source: Path | None = None) -> Path:
    versions = load_reference_case_versions(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(item.model_dump_json() + "\n" for item in versions), encoding="utf-8")
    return path


def embedded_reference_case(case: CaseVersion) -> ReferenceCase:
    raw = case.input.params.get("referenceCase")
    if not isinstance(raw, dict):
        raise ValueError(f"{case.caseId}:缺少 referenceCase")
    return ReferenceCase.model_validate(raw)
