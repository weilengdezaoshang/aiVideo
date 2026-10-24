"""Strict semantic self-check result and durable journal nested under CorePlan.self_check."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core.contracts import Contract

SCHEMA_VERSION = "selfcheck-v1"
SEMANTIC_KEY = "semantic"
TEMPLATE_VERSION = "selfcheck-v1"
# Frozen identity of parse/trigger/summary rules; not a quality score.
POLICY_ID = "selfcheck-policy-v1-strict-json-data-graph"
POLICY_HASH = hashlib.sha256(POLICY_ID.encode("utf-8")).hexdigest()

SemanticStatus = Literal[
    "reserved",
    "sending",
    "stored",
    "parsed",
    "rule_only",
    "unknown",
    "unavailable",
    "skipped",
    "blocked",
]
EnforcementMode = Literal["off", "shadow", "enforce"]
Provenance = Literal["live", "synthetic"]


class SemanticProblem(Contract):
    model_config = ConfigDict(extra="forbid")
    item: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    actual: str = Field(min_length=1)


class SemanticVerdict(Contract):
    """Wire JSON from the checker. Unknown fields or types are invalid."""

    model_config = ConfigDict(extra="forbid")
    pass_: bool = Field(alias="pass", strict=True)
    problems: list[SemanticProblem]

    @model_validator(mode="after")
    def negative_needs_evidence(self):
        if self.pass_ is False and not self.problems:
            raise ValueError("empty negative without evidence")
        return self


class TriggerPolicy(Contract):
    video_anywhere: bool = True
    min_steps: int = Field(default=8, ge=1)
    min_depth: int = Field(default=6, ge=1)
    cost_threshold: int = Field(default=80, ge=0)
    helper_marker: bool = True


class EnforcementEvidence(Contract):
    """Live baseline identity. synthetic/forged rows never unlock production enforce."""

    provenance: Provenance = "synthetic"
    route: dict[str, Any] = Field(default_factory=dict)
    route_id: str = ""
    model: str
    template_version: str
    template_hash: str
    policy_hash: str
    dataset_hash: str
    drift_count: int = Field(default=0, ge=0)
    normal_count: int = Field(default=0, ge=0)
    valid_observation_rate: float = Field(default=0, ge=0, le=1)
    fallback_count: int = Field(default=0, ge=0)
    observed_false_block_rate: float = Field(ge=0, le=1)
    case_count: int = Field(default=0, ge=0)
    llm_tested: bool = False


class SemanticJournal(BaseModel):
    """Nested under self_check. Mutable working copy; wire dump is JSON."""

    model_config = ConfigDict(extra="forbid")
    schema_version: str = SCHEMA_VERSION
    status: SemanticStatus = "skipped"
    mode: EnforcementMode = "off"
    triggered: bool = False
    triggers: list[str] = Field(default_factory=list)
    call_key: str = ""
    template_version: str = TEMPLATE_VERSION
    template_hash: str = ""
    policy_hash: str = POLICY_HASH
    policy_fingerprint: str = ""
    route: dict[str, Any] | None = None
    model: str | None = None
    input_hash: str = ""
    candidate_hash: str = ""
    catalog_hash: str = ""
    input_revision: int = 1
    invocation_id: str = ""
    raw: dict[str, Any] | None = None
    parsed: dict[str, Any] | None = None
    fallback_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    trace: dict[str, str | None] = Field(default_factory=dict)
    enforcement: dict[str, Any] = Field(default_factory=dict)
    degraded: bool = False
    request_messages: list[dict[str, Any]] | None = None
    provider_kind: str = "none"
    lease_token: str | None = None


class SelfCheckRaw(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    unknown: bool = False
