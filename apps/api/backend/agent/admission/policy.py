"""Three deterministic checks. Unsupported semantics require clarification, never optimism."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import Field, ValidationError

from backend.agent.media.contracts import Contract, MediaPlan, ReviewSettings
from backend.agent.media.planner import validate_plan
from backend.agent.media.intent.negation import affirmative_text
from backend.agent.media.intent.gates import apply_gates, WEAK_MOTION
from backend.agent.media.intent.feasibility import build_feasibility

REVIEW_VERSION = "admission-v1"
Writer = Literal["rule", "small", "llm", "skill", "helper", "user"]


class IntentContract(Contract):
    """Host-owned frozen constraints, not supplied alongside a model proposal.

    expectedPlan is produced by an existing deterministic template or an explicitly
    confirmed plan. A free-form proposal cannot mint its own intent contract.
    """
    message: str
    expectedPlan: dict
    readableAssets: list[str]
    writableNodes: list[str]
    inputReferences: dict[str, int] = Field(default_factory=dict)
    blocked: bool = False
    ambiguous: bool = False
    confirmedType: Literal["image", "video"] | None = None


class Proposal(Contract):
    writer: Writer
    plan: dict
    reason: str = "initial"
    modelCallId: str | None = None


class Issue(Contract):
    gate: Literal["legal", "types", "intent"]
    code: str
    path: str = "plan"


class Review(Contract):
    verdict: Literal["approved", "rejected", "clarify"]
    checks: dict[str, bool]
    issues: list[Issue]
    quote: int = 0
    version: str = REVIEW_VERSION


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def semantics(plan: MediaPlan) -> dict:
    value = plan.model_dump(mode="json")
    for step in value["steps"]:
        # Coordinates and display labels are owned by code. Node scope is checked separately.
        step.pop("targetNodes")
        step.pop("title")
    return value


def review(proposal: Proposal, intent: IntentContract, catalog, settings: ReviewSettings) -> Review:
    issues: list[Issue] = []
    def add(gate, code, path="plan"):
        issues.append(Issue(gate=gate, code=code, path=path))
    try:
        plan = MediaPlan.model_validate(proposal.plan)
    except ValidationError:
        return Review(verdict="rejected", checks={"legal": False, "types": False, "intent": False},
            issues=[Issue(gate="legal", code="invalid_plan")])
    if len(plan.steps) > settings.maxTasks or sum(s.fanOut for s in plan.steps) > settings.maxExpandedTasks:
        add("legal", "plan_limit")
    if not set(plan.sourceAssetIds) <= set(intent.readableAssets):
        add("legal", "asset_out_of_scope", "sourceAssetIds")
    if not {node for s in plan.steps for node in s.targetNodes} <= set(intent.writableNodes):
        add("legal", "node_out_of_scope", "steps.targetNodes")
    violations = validate_plan(plan, catalog)
    for violation in violations:
        gate = "types" if violation.code in {"plan_input_type_invalid", "plan_input_not_dependent",
            "plan_frame_count_invalid", "plan_unexpected_reference"} else "legal"
        add(gate, violation.code, violation.stepId or "plan")
    quote = 0
    if not violations:
        quote = catalog.quote(plan)
        if quote > settings.maxCredits:
            add("legal", "credit_budget_exceeded")
        by_id = {step.id: step for step in plan.steps}
        for step in plan.steps:
            cap = catalog.resolve(step.capabilityId, step.tier)
            roots = len(plan.sourceAssetIds) if step.capabilityId in {"image_edit", "image_to_video"} else 0
            count = len(step.inputFrom) if step.inputFrom else intent.inputReferences.get(step.id, roots)
            if count > cap.maxRefImages or (step.capabilityId in {"image_edit", "image_to_video"} and not count):
                add("types", "reference_type_or_count", step.id)
            for ref in step.inputFrom:
                upstream = catalog.resolve(by_id[ref.stepId].capabilityId, plan.tier)
                if upstream.outputType != "image":
                    add("types", "reference_type_mismatch", step.id)
    expected = MediaPlan.model_validate(intent.expectedPlan)
    if expected.goal.strip() != intent.message.strip():
        add("intent", "unconfirmed_prompt_rewrite")
    if semantics(plan) != semantics(expected):
        add("intent", "intent_contract_mismatch")
    positive = affirmative_text(intent.message)
    _, gates = apply_gates(intent.message, build_feasibility(bool(intent.readableAssets), catalog))
    video = "style_as_art" not in gates and bool(re.search(
        r"视频|动画(?!风|样式)|短片|\bvideo\b|\banimation(?![-\s]+style)\b", positive, re.I))
    if video and plan.goalType != "video":
        add("intent", "video_degradation")
    if "negative_video" in gates and plan.goalType == "video":
        add("intent", "unrequested_video")
    if intent.confirmedType and intent.confirmedType != plan.goalType:
        add("intent", "confirmed_type_mismatch")
    if intent.blocked:
        add("intent", "content_blocked")
    ambiguous = intent.ambiguous or "video_negation_conflict" in gates or (
        intent.confirmedType is None and not video and ("weak_motion" in gates or bool(re.search(
            WEAK_MOTION + r"|好看一点|优化一下", positive, re.I))))
    if ambiguous:
        add("intent", "intent_requires_clarification")
    checks = {gate: not any(i.gate == gate for i in issues) for gate in ("legal", "types", "intent")}
    if ambiguous and checks["legal"] and checks["types"] and not intent.blocked:
        verdict = "clarify"
    else:
        verdict = "rejected" if issues else "approved"
    return Review(verdict=verdict, checks=checks, issues=issues, quote=quote)
