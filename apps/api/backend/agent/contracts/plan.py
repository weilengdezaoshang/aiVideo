"""Plan, final-answer claims and reflection verdicts exchanged with the model."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .evidence import Contract

MAX_PLAN_STEPS = 6


class PlanStep(Contract):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    title: str = Field(min_length=1, max_length=80)
    intent: str = Field(min_length=1, max_length=400)
    tools: list[str] = Field(max_length=8)
    dependsOn: list[str] = Field(default_factory=list, max_length=MAX_PLAN_STEPS)
    doneWhen: str = Field(min_length=1, max_length=300)


class Plan(Contract):
    goal: str = Field(min_length=1, max_length=300)
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_PLAN_STEPS)


ClaimScope = Literal["complete", "partial", "item", "unavailable"]


class Claim(Contract):
    text: str = Field(min_length=1, max_length=400)
    evidenceRefs: list[str] = Field(max_length=8)
    scope: ClaimScope


class Finish(Contract):
    answer: str = Field(min_length=1, max_length=4000)
    claims: list[Claim] = Field(max_length=32)


IssueKind = Literal["ungrounded", "incomplete", "truncated", "off_target", "stale"]


class ReflectionIssue(Contract):
    kind: IssueKind
    problem: str = Field(min_length=1, max_length=300)
    stepKey: str | None = None
    evidenceRefs: list[str] = Field(default_factory=list, max_length=8)


class Reflection(Contract):
    verdict: Literal["accept", "revise_answer", "retry_step", "replan", "ask_user"]
    issues: list[ReflectionIssue] = Field(default_factory=list, max_length=8)
    question: str | None = Field(default=None, max_length=300)
