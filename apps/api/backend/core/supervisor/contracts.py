"""Shared planner contract and all supervisor tuning parameters."""
from typing import Literal, Protocol
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from backend.core.contracts import Contract


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_SUPERVISOR_", frozen=True)
    review_retries: int = Field(default=2, ge=0)
    confidence_threshold: float = Field(default=0.8, ge=0, le=1)
    lease_seconds: float = Field(default=30, gt=0)
    planner_timeout_seconds: float = Field(default=10, gt=0)
    concurrency: int = Field(default=4, gt=0)
    scan_limit: int = Field(default=100, gt=0)
    context_messages: int = Field(default=30, gt=0)
    recent_output_limit: int = Field(default=8, gt=0)
    feedback_max_chars: int = Field(default=2000, gt=0)
    context_max_objects: int = Field(default=24, gt=0)
    context_max_chars: int = Field(default=6000, gt=0)
    context_text_snippet_chars: int = Field(default=160, gt=0)
    planner_tool_threshold: int = Field(default=20, gt=0)
    planner_tool_candidates: int = Field(default=8, gt=0)
    planner_retrieval_version: str = "bm25-v1"
    planner_bm25_k1: float = Field(default=1.5, gt=0)
    planner_bm25_b: float = Field(default=0.75, ge=0, le=1)
    max_tasks: int = Field(default=100, gt=0)
    lock_timeout_ms: int = Field(default=100, gt=0)
    retry_seconds: float = Field(default=1, gt=0)
    planner_failures: int = Field(default=3, gt=0)
    planner_checkpoint_margin_seconds: float = Field(default=15, ge=0)
    viewport_span_limit: float = Field(default=100_000, gt=0)
    viewport_origin_limit: float = Field(default=1_000_000, gt=0)
    card_context_limit: int = Field(default=12, gt=0)


class Step(Contract):
    id: str = Field(pattern=r"^t[1-9][0-9]*$")
    kind: Literal["call_tool", "ask_user"] = "call_tool"
    tool: str | None = None
    parameters: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    placement: dict = Field(default_factory=dict)


class EvidenceItem(Contract):
    """Quoted support for a control call. The planner records it; this module does not judge it."""

    source: str = ""
    quote: str = ""


class FormField(Contract):
    """Structured question. `name` remains the legacy alias of `id`."""

    name: str = ""
    id: str = ""
    label: str = ""
    required: bool = False
    type: Literal["text", "number", "boolean", "choice"] = "text"
    default: str | int | float | bool | None = None
    choices: list[str] = Field(default_factory=list)


class FormDefinition(Contract):
    """ask_user_form shape. Empty means the plan did not ask."""

    id: str = ""
    fields: list[FormField] = Field(default_factory=list)


class ControlCallAudit(Contract):
    """Audit record for a control call (set_invariants / ask_user_form / answer_only)."""

    tool: str = ""
    arguments: dict = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    form: FormDefinition = Field(default_factory=FormDefinition)


class Plan(Contract):
    goal: str
    steps: list[Step] = Field(default_factory=list)
    result_type: Literal["generate", "answer_only", "clarify"] = "generate"
    writer: Literal["rule", "small", "llm", "skill", "helper", "user"] = "rule"
    answer: str = ""
    control_audit: ControlCallAudit = Field(default_factory=ControlCallAudit)
    control_audits: list[ControlCallAudit] = Field(default_factory=list)

    @model_validator(mode="after")
    def empty_steps_only_for_answer(self):
        if not self.steps and self.result_type != "answer_only":
            raise ValueError("empty steps are only valid for answer_only")
        return self


class PlannerResult(Contract):
    plan: Plan
    invariants: dict = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    raw_output: dict
    tokens: int = Field(default=0, ge=0)


class Planner(Protocol):
    async def plan(self, context: dict) -> PlannerResult: ...
