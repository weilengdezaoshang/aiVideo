"""Labeled LLM planner cases: schema and JSONL loading only.

This module does not call a model, score a live plan, or read provider credentials.
A future harness may adapt `recent_outputs` and `snapshot` into its own planner view.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal[
    "single",
    "ordered_multi",
    "reference",
    "ambiguous",
    "chat",
    "unsupported",
    "video_missing",
    "injection",
]
Language = Literal["zh", "en"]
ResultType = Literal["generate", "answer_only", "clarify"]
MediaKind = Literal["image", "video", "text"]

CATEGORIES: tuple[str, ...] = (
    "single",
    "ordered_multi",
    "reference",
    "ambiguous",
    "chat",
    "unsupported",
    "video_missing",
    "injection",
)

# Capability names already published by the tool catalog. ask_user_form and answer_only
# are planner outcomes, not billed generation tools. set_invariants is not a tool choice.
CAPABILITY_TOOLS: frozenset[str] = frozenset({
    "text_to_image_preview",
    "image_edit_preview",
    "text_to_video_preview",
    "image_to_video_preview",
    "detect_subject",
    "cutout",
    "read_canvas",
    "read_asset",
})
PLANNER_OUTCOMES: frozenset[str] = frozenset({"ask_user_form", "answer_only"})
KNOWN_TOOLS: frozenset[str] = CAPABILITY_TOOLS | PLANNER_OUTCOMES
VIDEO_TOOLS: frozenset[str] = frozenset({
    "text_to_video_preview",
    "image_to_video_preview",
})
DELIVERABLE_FIELDS: frozenset[str] = frozenset({
    "count",
    "modality",
    "aspect_ratio",
    "width",
    "height",
    "duration_seconds",
    "required_asset_ids",
})

FIXTURE_PATH = (
    Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "llm-planner-cases.jsonl"
)


class RecentOutput(BaseModel):
    """One finished product the adapter may later map into planner memory.

    `rank` 1 is the newest output. `output_id` is a dataset label, not a database id.
    """

    model_config = ConfigDict(extra="forbid")

    output_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    modality: Literal["image", "video"]
    label: str = Field(min_length=1)
    rank: int = Field(ge=1)


class SnapshotObject(BaseModel):
    """Canvas object. `text` is visible object text, including injection fixtures."""

    model_config = ConfigDict(extra="forbid")

    object_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    type: MediaKind
    text: str = ""
    label: str = ""


class SnapshotAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    kind: MediaKind
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


class RequestView(BaseModel):
    """User request. An injection row repeats its control row's request unchanged."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    selected_assets: list[str] = Field(default_factory=list)


class ScopeView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readable_assets: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class SnapshotView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objects: list[SnapshotObject] = Field(default_factory=list)
    assets: list[SnapshotAsset] = Field(default_factory=list)


class DeliverableLabel(BaseModel):
    """Only fields the user stated. Absent fields are omitted, never invented."""

    model_config = ConfigDict(extra="forbid")

    count: int | None = Field(default=None, ge=1)
    modality: Literal["image", "video"] | None = None
    aspect_ratio: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    duration_seconds: int | None = Field(default=None, ge=1)
    required_asset_ids: list[str] = Field(default_factory=list)


class InvariantsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deliverable: DeliverableLabel | None = None


class CaseContext(BaseModel):
    """Frozen inputs. `invariants` here is the incoming run, not the expected label."""

    model_config = ConfigDict(extra="forbid")

    request: RequestView
    snapshot: SnapshotView
    summary: str = ""
    recent_outputs: list[RecentOutput] = Field(default_factory=list)
    invariants: InvariantsView = Field(default_factory=InvariantsView)
    scope: ScopeView
    upload_note: str | None = None


class ExpectedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_names: list[str] = Field(min_length=1)
    dependencies: dict[str, list[str]] = Field(default_factory=dict)
    invariants: InvariantsView = Field(default_factory=InvariantsView)
    result_type: ResultType
    must_ask: list[str] = Field(default_factory=list)
    bindings: dict[str, str] = Field(default_factory=dict)


class PlannerCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    category: Category
    language: Language
    pair_id: str | None = None
    context: CaseContext
    expected: ExpectedPlan


def load_cases(path: Path | None = None) -> list[PlannerCase]:
    """Load the labeled JSONL fixture. Raises if a row does not match the schema."""

    source = path or FIXTURE_PATH
    cases: list[PlannerCase] = []
    for line_number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            cases.append(PlannerCase.model_validate(json.loads(raw)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{source}:{line_number}: {exc}") from exc
    return cases
