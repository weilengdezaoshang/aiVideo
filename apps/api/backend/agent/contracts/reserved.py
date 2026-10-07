"""Reserved evidence kinds of the generation loop (next round): schema only.

Nothing in this round produces them. ``ApprovalEvidence`` in particular can only
be written by the Runtime from an explicit user action; no tool schema exposes a
path that would let a model fabricate one.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .evidence import Contract


class UsageRef(Contract):
    """The single canvas slot an artifact's bytes may occupy."""

    documentId: str
    objectId: str


class ArtifactEvidence(Contract):
    kind: Literal["artifact"] = "artifact"
    artifactId: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sourceChain: list[str] = Field(default_factory=list)
    usageRef: UsageRef


class CheckEvidence(Contract):
    kind: Literal["check"] = "check"
    constraintId: str
    method: str
    result: Literal["pass", "fail", "inconclusive"]
    observed: dict[str, Any]
    crops: list[str] = Field(default_factory=list)


class ApprovalEvidence(Contract):
    """User approval of one artifact under one recipe and quote; Runtime-written only."""

    kind: Literal["approval"] = "approval"
    artifactId: str
    recipe: str
    quote: dict[str, int]
    approvedVersion: int = Field(ge=1)


class ValidationIssue(Contract):
    check: str
    code: str
    detail: str
    ref: str | None = None


class ValidationCheck(Contract):
    id: str
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class ValidationReport(Contract):
    kind: Literal["validation"] = "validation"
    recipe: str
    passed: bool
    checks: list[ValidationCheck]
