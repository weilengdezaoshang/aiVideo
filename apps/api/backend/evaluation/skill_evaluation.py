"""Skill playbook evaluation: selection chain + reviewer verdicts, pure rules, no DB.

Each case observes two things against the official playbooks bundled in
``core/skills/official``: which skill the selection chain picks for the message,
and whether the plan survives ``review.validate`` under the picked skill's
tightened scope and compiled intent rules. Grading compares both with the label;
an unknown observation is never counted as a pass.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from backend.core.review import validate
from backend.core.review.contracts import ParamRule, ToolSpec
from backend.core.skills.compile_rules import skill_intent_rules
from backend.core.skills.contracts import SkillSpec
from backend.core.skills.selection import SkillView, match_skill
from backend.core.skills.store import seed_official_skills

BIG_BUDGET = 10 ** 6

# Minimal frozen tool table covering every tool named by any playbook or case.
# The declared prompt rule keeps case params legal and task fingerprints distinct.
TOOL_TABLE: dict[str, ToolSpec] = {
    name: ToolSpec(id=name, version="1", output_type=output,
                   params_schema={"prompt": ParamRule(type="string")})
    for name, output in {
        "detect_subject": "mask", "cutout": "image",
        "image_edit_preview": "image", "image_edit_final": "image",
        "text_to_image_preview": "image", "text_to_image_final": "image",
        "text_to_video": "video", "text_to_video_preview": "video", "text_to_video_final": "video",
        "image_to_video": "video", "image_to_video_preview": "video", "image_to_video_final": "video",
        "frames_to_video": "video", "frames_to_video_preview": "video",
        "video_concat": "video", "image_analyze": "text",
        "create_asset_card": "text", "update_asset_card": "text",
    }.items()
}


class SkillCase(BaseModel):
    """One labeled observation over the bundled official playbooks."""

    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    skill: str  # Which playbook's suite the case belongs to.
    category: str  # selection / allowlist / phase / confirmation / good / none
    user_message: str = Field(min_length=1)
    explicit_skill: str | None = None
    expected_skill: str | None = None
    plan: dict
    label_pass: bool
    expected_codes: list[str] = Field(default_factory=list)


def official_specs() -> dict[str, SkillSpec]:
    return {spec.name: spec for _, spec in seed_official_skills()}


def _views(specs: dict[str, SkillSpec]) -> list[SkillView]:
    return [SkillView.of(name, name, spec) for name, spec in specs.items()]


def observe_case(case: SkillCase) -> dict:
    """Run the selection chain and the reviewer; both outcomes are recorded."""
    specs = official_specs()
    picked = match_skill(_views(specs), case.user_message, explicit=case.explicit_skill)
    selection_ok = (picked.name if picked else None) == case.expected_skill
    spec = specs.get(case.expected_skill) if case.expected_skill else None
    context = {"user_id": "eval", "user_credits": BIG_BUDGET, "run_budget": BIG_BUDGET,
        "scope": {"tools": list(spec.allowlist) if spec else list(TOOL_TABLE),
                  "readable_assets": [], "writable_regions": []},
        "version_lock": {"tools": {name: tool.model_dump() for name, tool in TOOL_TABLE.items()}},
        "invariants": {}, "canvas": {"assets": [], "nodes": []}}
    result = validate(case.plan, context,
        intent_extensions=skill_intent_rules(spec) if spec else None)
    codes = [reason.code for reason in result.reasons]
    return {"case_id": case.case_id, "selected": picked.name if picked else None,
        "expected_skill": case.expected_skill, "selection_ok": selection_ok,
        "verdict": result.verdict, "codes": codes, "gates": sorted({reason.gate for reason in result.reasons}),
        "observed_pass": result.verdict == "pass" and selection_ok}


def grade_case(case: SkillCase, observation: dict) -> dict:
    """Honest grading: label mismatch or an unexpected code set is a failure."""
    passed = observation["observed_pass"] == case.label_pass
    if case.label_pass:
        passed = passed and not observation["codes"]
    elif case.expected_codes:
        passed = passed and all(code in observation["codes"] for code in case.expected_codes)
    return {"case_id": case.case_id, "passed": passed,
            "reason": "" if passed else _mismatch(case, observation)}


def _mismatch(case: SkillCase, observation: dict) -> str:
    if not observation["selection_ok"]:
        return f"selection: picked {observation['selected']} want {case.expected_skill}"
    if observation["observed_pass"] != case.label_pass:
        return f"verdict: {observation['verdict']} codes={observation['codes']}"
    if case.expected_codes:
        missing = [code for code in case.expected_codes if code not in observation["codes"]]
        return f"codes missing {missing}, got {observation['codes']}"
    return f"unexpected codes {observation['codes']}"


def evaluate_cases(cases: list[SkillCase]) -> dict:
    observations = [observe_case(case) for case in cases]
    grades = [grade_case(case, observation) for case, observation in zip(cases, observations)]
    by_skill: dict[str, dict] = {}
    for case, grade in zip(cases, grades):
        bucket = by_skill.setdefault(case.skill, {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += int(grade["passed"])
    return {"total": len(cases), "passed": sum(g["passed"] for g in grades),
        "failed": sum(1 for g in grades if not g["passed"]),
        "by_skill": by_skill, "grades": grades, "observations": observations}


def suite_paths(output: Path, summary: dict) -> list[Path]:
    import json
    output.mkdir(parents=True, exist_ok=True)
    observations = output / "observations.jsonl"
    observations.write_text("\n".join(json.dumps(row, ensure_ascii=False)
        for row in summary["observations"]) + "\n", encoding="utf-8")
    slim = {key: value for key, value in summary.items() if key != "observations"}
    slim_path = output / "summary.json"
    slim_path.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    return [observations, slim_path]
