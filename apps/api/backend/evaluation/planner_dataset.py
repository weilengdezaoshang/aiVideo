"""Import labeled planner rows into the existing CaseVersion / DatasetStore contract."""

from __future__ import annotations

import json
from pathlib import Path

from backend.evaluation.models import CaseInput, CaseVersion, Check, Provenance
from backend.evaluation.planner_cases import FIXTURE_PATH, PlannerCase, load_cases

DATASET_ID = "llm-planner"


def planner_checks(case: PlannerCase) -> list[Check]:
    """Boolean checks. Expected values are the pass condition, not a forged observation."""

    ordered = bool(case.expected.dependencies)
    return [
        Check(id="tool_match", kind="boolean", question="工具序列与标注一致", expected=True),
        Check(
            id="order_match",
            kind="boolean",
            question="依赖边与步骤先后一致",
            expected=True,
            required=ordered,
        ),
        Check(id="invariants_match", kind="boolean", question="交付约束与标注一致", expected=True),
        Check(id="bindings_match", kind="boolean", question="参数绑定与标注一致", expected=True),
        Check(id="must_ask_match", kind="boolean", question="澄清项覆盖标注字段", expected=True),
        Check(
            id="first_review_pass",
            kind="boolean",
            question="首次审核回执为 pass",
            expected=True,
        ),
    ]


def case_version_from_planner(case: PlannerCase) -> CaseVersion:
    if case.case_id == "agent-flow" or "agent_flow" in case.case_id:
        raise ValueError(f"planner 用例不能使用 agent_flow 身份:{case.case_id}")
    return CaseVersion(
        caseId=case.case_id,
        language="zh-CN" if case.language == "zh" else "en",
        taskType="planner",
        tags=[case.category, "planner"],
        suites=["planner", "regression"],
        title=case.case_id,
        input=CaseInput(
            prompt=case.context.request.message,
            params={
                "kind": "planner",
                "plannerCase": case.model_dump(mode="json"),
            },
        ),
        checks=planner_checks(case),
        provenance=Provenance(source="imported", note="tests/fixtures/llm-planner-cases.jsonl"),
    )


def load_planner_case_versions(path: Path | None = None) -> list[CaseVersion]:
    cases = load_cases(path or FIXTURE_PATH)
    versions = [case_version_from_planner(case) for case in cases]
    if len(versions) != len(cases):
        raise ValueError("planner 导入丢行")
    return versions


def write_planner_dataset(path: Path, source: Path | None = None) -> Path:
    versions = load_planner_case_versions(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(item.model_dump_json() + "\n" for item in versions),
        encoding="utf-8",
    )
    return path


def embedded_planner_case(case: CaseVersion) -> PlannerCase:
    raw = case.input.params.get("plannerCase")
    if not isinstance(raw, dict):
        raise ValueError(f"{case.caseId}:缺少 plannerCase")
    return PlannerCase.model_validate(raw)


def read_observation(path: Path) -> dict:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"观测不是对象:{path}")
    return payload
