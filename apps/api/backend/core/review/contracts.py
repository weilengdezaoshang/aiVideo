"""Frozen data contracts for the plan reviewer; every input is plain JSON-compatible data.

The reviewer never opens a database, calls a model or mutates the plan. The run context is
assembled by the host from the frozen Run row (scope / invariants / version_lock) plus the
current user balance and canvas snapshot, so validate() stays a pure function.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from backend.core.contracts import Contract

REVIEW_VERSION = "core-review-v1"

AssetKind = Literal["image", "video", "audio", "mask", "text"]
GateName = Literal["legal", "types", "intent"]

TASK_ID_PATTERN = r"^t[1-9][0-9]*$"
_TASK_OUTPUT_REF = re.compile(r"^\$(t[1-9][0-9]*)\.output$")


class TaskRef(Contract):
    """Reference to the media output of an upstream step, written `$tN.output` in plan JSON."""

    task: str = Field(pattern=TASK_ID_PATTERN)


class AssetRef(Contract):
    asset_id: str = Field(min_length=1)
    version_id: str | None = None


def _coerce_input(value):
    if isinstance(value, str):
        match = _TASK_OUTPUT_REF.fullmatch(value)
        if match is None:
            raise ValueError(f"输入引用必须是 $tN.output 或资产对象，收到：{value}")
        return {"task": match.group(1)}
    return value


InputRef = Annotated[Union[TaskRef, AssetRef], BeforeValidator(_coerce_input)]


class ParamRule(Contract):
    """Minimal JSON-schema subset; unknown parameters and out-of-range values fail closed."""

    type: Literal["number", "string", "boolean"] | None = None
    enum: list | None = None
    minimum: float | None = None
    maximum: float | None = None
    required: bool = False


class ToolSpec(Contract):
    """A tool definition frozen inside version_lock; the reviewer never reads a live catalog."""

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    capability: str = ""
    enabled: bool = True
    output_type: AssetKind | None = None
    input_types: dict[str, list[AssetKind]] = Field(default_factory=dict)
    params_schema: dict[str, ParamRule] = Field(default_factory=dict)
    # Video tools only: parameters that mark the ticket as undecided when absent. A missing
    # parameter here is only tolerated when an ask_user step runs upstream of this step.
    ambiguous_without: list[str] = Field(default_factory=list)
    cost_credits: int = Field(default=0, ge=0)


class VersionLock(Contract):
    tools: dict[str, ToolSpec] = Field(default_factory=dict)


class Scope(Contract):
    """Run-scoped whitelist: tool ids (optionally `id@version`), readable assets and regions."""

    tools: list[str] = Field(default_factory=list)
    readable_assets: list[str] = Field(default_factory=list)
    writable_regions: list[str] = Field(default_factory=list)


class CanvasAsset(Contract):
    asset_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    kind: AssetKind
    version_id: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    duration_seconds: int | None = Field(default=None, ge=1)


class CanvasNode(Contract):
    id: str = Field(min_length=1)


class CanvasSnapshot(Contract):
    nodes: list[CanvasNode] = Field(default_factory=list)
    assets: list[CanvasAsset] = Field(default_factory=list)


class DeliverableInvariant(Contract):
    """User-facing expectations the final deliverables must satisfy, item by item."""

    count: int | None = Field(default=None, ge=1)
    modality: Literal["image", "video"] | None = None
    aspect_ratio: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    duration_seconds: int | None = Field(default=None, ge=1)
    required_asset_ids: list[str] = Field(default_factory=list)


class Invariants(Contract):
    deliverable: DeliverableInvariant | None = None


class PlanStep(Contract):
    id: str = Field(pattern=TASK_ID_PATTERN)
    kind: Literal["call_tool", "ask_user"] = "call_tool"
    tool: str | None = None
    tool_version: str | None = None
    params: dict = Field(default_factory=dict)
    inputs: dict[str, InputRef] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    region: str | None = None
    estimated_cost: int | None = Field(default=None, ge=0)


class Plan(Contract):
    """Model- or user-written ticket list. The reviewer treats every writer identically."""

    goal: str = ""
    steps: list[PlanStep] = Field(min_length=1)


class ReplanContext(Contract):
    """Frozen previous plan version plus the task ids that ended blocked_by_model."""

    previous: Plan
    blocked_task_ids: list[str] = Field(default_factory=list)


class RunContext(Contract):
    user_id: str = Field(min_length=1)
    team_user_ids: list[str] = Field(default_factory=list)
    user_credits: int = Field(ge=0)
    run_budget: int = Field(ge=0)
    scope: Scope = Field(default_factory=Scope)
    version_lock: VersionLock = Field(default_factory=VersionLock)
    invariants: Invariants = Field(default_factory=Invariants)
    canvas: CanvasSnapshot = Field(default_factory=CanvasSnapshot)
    replan: ReplanContext | None = None


class ReviewReason(Contract):
    """One model-readable rejection sentence; `step` names the offending ticket when known."""

    gate: GateName
    code: str
    step: str | None = None
    message: str = Field(min_length=1)


class ReviewResult(Contract):
    verdict: Literal["pass", "reject"]
    reasons: list[ReviewReason] = Field(default_factory=list)
    estimated_cost: int = 0
    version: str = REVIEW_VERSION


class PlanView(BaseModel):
    """Resolved read-only view handed to gate-③ extension rules (block 11 plugs in here)."""

    model_config = ConfigDict(frozen=True)
    plan: Plan
    context: RunContext
    tools: dict[str, ToolSpec | None]
    deliverable_ids: tuple[str, ...]
