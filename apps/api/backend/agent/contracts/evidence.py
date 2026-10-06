"""Tool result envelopes: facts the model may cite and next steps it may take.

Every envelope is data produced by the harness, never by the model. Canvas text only
ever appears as a ``Fact.value`` or inside ``data``; statements are fixed templates.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(Contract):
    documentId: str
    revision: int
    assetIds: list[str] = Field(default_factory=list)


class Omitted(Contract):
    objectIds: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)


class Page(Contract):
    offset: int = Field(ge=0)
    nextOffset: int | None = Field(ge=0)
    total: int = Field(ge=0)


FactValue = str | int | float | bool | list[int | float | str]


class Fact(Contract):
    id: str = Field(pattern=r"^E\d+\.f\d+$")
    statement: str
    path: str
    value: FactValue
    scope: Literal["complete", "partial", "item"]


class Coverage(Contract):
    complete: bool
    reason: str | None = None
    missing: Omitted | None = None


class NextAction(Contract):
    action: Literal["call_tool", "complete_step", "ask_user", "report_blocker"]
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    reason: str
    priority: Literal["required", "suggested"]


MAX_NEXT_ACTIONS = 5


class FieldProblem(Contract):
    path: str
    problem: Literal["type", "missing", "extra", "range", "value"]


class FailureDetails(Contract):
    fields: list[FieldProblem] = Field(default_factory=list)
    unknownObjectIds: list[str] = Field(default_factory=list)
    allowedTools: list[str] = Field(default_factory=list)
    limit: dict[str, int] | None = None


FailureCategory = Literal["invalid_arguments", "out_of_scope", "unavailable", "changed",
                          "limit", "policy", "internal"]


class ToolFailure(Contract):
    code: str
    category: FailureCategory
    message: str
    retryable: bool
    details: FailureDetails = Field(default_factory=FailureDetails)
    next: list[NextAction] = Field(default_factory=list, max_length=MAX_NEXT_ACTIONS)


class OkResult(Contract):
    data: dict[str, Any]
    evidence: list[Fact]
    coverage: Coverage
    next: list[NextAction] = Field(default_factory=list, max_length=MAX_NEXT_ACTIONS)
    truncated: bool
    omitted: Omitted = Field(default_factory=Omitted)
    page: Page | None = None


class EnvelopeBase(Contract):
    evidenceId: str = Field(pattern=r"^E\d+$")
    kind: Literal["tool"] = "tool"
    tool: str
    toolVersion: int
    callId: str
    stepKey: str | None = None
    trust: Literal["untrusted_canvas_data"] = "untrusted_canvas_data"
    source: Source
    producedAt: str


class OkEnvelope(EnvelopeBase):
    status: Literal["ok"] = "ok"
    result: OkResult


class ErrorEnvelope(EnvelopeBase):
    status: Literal["error"] = "error"
    error: ToolFailure


class DuplicateEnvelope(EnvelopeBase):
    status: Literal["duplicate"] = "duplicate"
    dedupeOf: str = Field(pattern=r"^E\d+$")
    next: list[NextAction] = Field(default_factory=list, max_length=MAX_NEXT_ACTIONS)


Envelope = Annotated[OkEnvelope | ErrorEnvelope | DuplicateEnvelope, Field(discriminator="status")]


class ControlReceipt(Contract):
    """Reply to a control tool. It is not evidence and can never be cited."""
    kind: Literal["control"] = "control"
    tool: str
    callId: str
    status: Literal["accepted", "rejected"]
    effect: dict[str, Any] | None = None
    error: ToolFailure | None = None
