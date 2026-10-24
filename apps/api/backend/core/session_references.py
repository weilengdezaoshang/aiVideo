"""Resolve session artifact references from caller-supplied geometry. No model coordinates."""
from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.core.contracts import Contract


class ReferenceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_REFERENCES_", frozen=True, extra="forbid")
    candidate_limit: int = Field(default=32, gt=0)
    spatial_epsilon: float = Field(default=0.5, ge=0)
    prefer_selection: bool = True


class ArtifactCandidate(Contract):
    artifact_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    batch_id: str | None = None
    batch_index: int | None = Field(default=None, ge=1)
    asset_id: str = Field(min_length=1)
    permitted: bool
    available: bool
    selected: bool = False
    x: float | None = None
    y: float | None = None

    @field_validator("x", "y")
    @classmethod
    def finite_geometry(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("geometry must be finite")
        return value


class ReferenceQuery(Contract):
    caller_session_id: str = Field(min_length=1)
    kind: Literal["selected", "latest", "ordinal", "spatial"]
    batch_id: str | None = None
    ordinal: int | None = Field(default=None, ge=1)
    spatial: Literal["left", "right"] | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.kind == "ordinal" and self.ordinal is None:
            raise ValueError("ordinal reference requires an ordinal")
        if self.kind == "spatial" and self.spatial is None:
            raise ValueError("spatial reference requires left or right")
        if self.kind != "spatial" and self.spatial is not None:
            raise ValueError("spatial is only valid for spatial references")
        if self.kind != "ordinal" and self.ordinal is not None:
            raise ValueError("ordinal is only valid for ordinal references")
        return self


class ReferenceResolution(Contract):
    status: Literal["resolved", "ambiguous", "unavailable"]
    artifact_id: str | None = None
    asset_id: str | None = None
    reason: str
    candidate_ids: list[str] = Field(default_factory=list)


def _result(status, reason, items: list[ArtifactCandidate], *, reveal: bool) -> ReferenceResolution:
    chosen = items[0] if status != "ambiguous" and len(items) == 1 else None
    show = items if reveal else []
    return ReferenceResolution(
        status=status, reason=reason,
        artifact_id=chosen.artifact_id if chosen else None,
        asset_id=chosen.asset_id if chosen else None,
        candidate_ids=[item.artifact_id for item in show],
    )


def resolve(artifacts: list[ArtifactCandidate], query: ReferenceQuery,
            settings: ReferenceSettings | None = None) -> ReferenceResolution:
    settings = settings or ReferenceSettings()
    own = [item for item in artifacts if item.session_id == query.caller_session_id]
    if not own and artifacts:
        return _result("unavailable", "cross_session_isolated", [], reveal=False)
    permitted = [item for item in own if item.permitted]
    if own and not permitted:
        return _result("unavailable", "not_permitted", [], reveal=False)
    if len(permitted) > settings.candidate_limit:
        return _result("ambiguous", "candidate_limit", permitted[: settings.candidate_limit], reveal=True)

    selected = [item for item in permitted if item.selected]
    if settings.prefer_selection and selected and query.kind != "selected":
        return _finish_selected(selected)
    if query.kind == "selected":
        return _finish_selected(selected)
    if query.kind == "latest":
        return _finish_latest(permitted)
    if query.kind == "ordinal":
        return _finish_ordinal(permitted, query)
    return _finish_spatial(permitted, query, settings)


def _finish_selected(selected: list[ArtifactCandidate]) -> ReferenceResolution:
    if not selected:
        return _result("unavailable", "no_selection", [], reveal=False)
    if len(selected) > 1:
        readable = [item for item in selected if item.available]
        pool = readable or selected
        return _result("ambiguous", "multiple_selected", pool, reveal=True)
    item = selected[0]
    if not item.available:
        return _result("unavailable", "asset_unavailable", [item], reveal=True)
    return _result("resolved", "selected", [item], reveal=True)


def _finish_latest(permitted: list[ArtifactCandidate]) -> ReferenceResolution:
    if not permitted:
        return _result("unavailable", "no_artifact", [], reveal=False)
    item = max(permitted, key=lambda candidate: candidate.ordinal)
    if not item.available:
        return _result("unavailable", "asset_unavailable", [item], reveal=True)
    return _result("resolved", "latest", [item], reveal=True)


def _finish_ordinal(permitted: list[ArtifactCandidate], query: ReferenceQuery) -> ReferenceResolution:
    if query.batch_id is None:
        batch_ids = {item.batch_id for item in permitted if item.batch_id}
        if len(batch_ids) != 1:
            return _result("ambiguous", "ordinal_batch_unspecified", [], reveal=False)
        batch_id = next(iter(batch_ids))
    else:
        batch_id = query.batch_id
    batch = [item for item in permitted if item.batch_id == batch_id]
    if not batch:
        return _result("unavailable", "no_artifact", [], reveal=False)
    match = [item for item in batch if item.batch_index == query.ordinal]
    if not match:
        return _result("unavailable", "ordinal_out_of_range", [], reveal=False)
    if len(match) > 1:
        return _result("ambiguous", "ordinal_tie", match, reveal=True)
    item = match[0]
    if not item.available:
        return _result("unavailable", "asset_unavailable", [item], reveal=True)
    return _result("resolved", "ordinal", [item], reveal=True)


def _finish_spatial(permitted: list[ArtifactCandidate], query: ReferenceQuery,
                    settings: ReferenceSettings) -> ReferenceResolution:
    placed = [item for item in permitted if item.available and item.x is not None]
    if not placed:
        missing = [item for item in permitted if item.x is None and item.available]
        if missing and not any(item.x is not None for item in permitted):
            return _result("unavailable", "missing_geometry", [], reveal=False)
        return _result("unavailable", "no_artifact", [], reveal=False)
    extreme = min(item.x for item in placed) if query.spatial == "left" else max(item.x for item in placed)
    near = [item for item in placed if abs(item.x - extreme) <= settings.spatial_epsilon]
    if len(near) > 1:
        return _result("ambiguous", "spatial_tie", near, reveal=True)
    return _result("resolved", "spatial", near, reveal=True)
