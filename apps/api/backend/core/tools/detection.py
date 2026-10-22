"""Pure recognition output adapter, shared by manual flows and future workers.

No inference, charging, asset writes, mask persistence or candidate selection here.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Literal

from PIL import Image, UnidentifiedImageError
from pydantic import Field, model_validator

from .contracts import Contract, ToolSettings


class DetectionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ImageIdentity(Contract):
    asset_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class MaskReference(Contract):
    media_type: Literal["mask"] = "mask"
    handle: str = Field(min_length=1)
    source_asset_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    operation_id: str = Field(min_length=1)


class Candidate(Contract):
    mask: MaskReference
    label: str = Field(min_length=1)
    confidence: float | None = Field(ge=0, le=1)
    confidence_source: Literal["provider", "unknown"]

    @model_validator(mode="after")
    def known_confidence(self):
        if (self.confidence is None) != (self.confidence_source == "unknown"):
            raise ValueError("confidence source and value disagree")
        return self


class DetectionResult(Contract):
    result_type: Literal["detected", "no_subject"]
    candidate_count: int = Field(ge=0)
    granularity: Literal["combined_foreground", "instances"]
    candidates: list[Candidate]

    @model_validator(mode="after")
    def counts_match(self):
        if self.candidate_count != len(self.candidates):
            raise ValueError("candidate count mismatch")
        if (self.result_type == "no_subject") != (self.candidate_count == 0):
            raise ValueError("result type and candidates disagree")
        return self


@dataclass(frozen=True)
class RawCandidate:
    mask_png: bytes
    handle: str
    label: str
    confidence: float | None


def _has_foreground(mask_png: bytes, source: ImageIdentity, settings: ToolSettings) -> bool:
    if not mask_png or len(mask_png) > settings.max_mask_bytes:
        raise DetectionError("mask_invalid")
    if source.width * source.height > settings.max_image_pixels:
        raise DetectionError("mask_invalid")
    try:
        with Image.open(io.BytesIO(mask_png)) as mask:
            if mask.format != "PNG" or mask.mode != "L":
                raise DetectionError("mask_invalid")
            if mask.size != (source.width, source.height):
                raise DetectionError("mask_source_mismatch")
            mask.load()
            return mask.getbbox() is not None
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, DetectionError):
            raise
        raise DetectionError("mask_invalid") from exc


def normalize_candidates(raw: list[RawCandidate], source: ImageIdentity, operation_id: str, *,
                         granularity: str = "instances", settings: ToolSettings | None = None) -> DetectionResult:
    settings = settings or ToolSettings()
    if len(raw) > settings.max_candidates:
        raise DetectionError("candidate_limit")
    candidates = []
    handles = set()
    for item in raw:
        if not _has_foreground(item.mask_png, source, settings):
            continue
        if item.handle in handles:
            raise DetectionError("duplicate_mask_handle")
        handles.add(item.handle)
        candidates.append(Candidate(mask=MaskReference(handle=item.handle,
            source_asset_id=source.asset_id, source_sha256=source.sha256, width=source.width,
            height=source.height, operation_id=operation_id), label=item.label,
            confidence=item.confidence, confidence_source="unknown" if item.confidence is None else "provider"))
    return DetectionResult(result_type="detected" if candidates else "no_subject",
        candidate_count=len(candidates), granularity=granularity, candidates=candidates)


def normalize_rembg(mask_png: bytes, source: ImageIdentity, operation_id: str, mask_handle: str, *,
                    settings: ToolSettings | None = None) -> DetectionResult:
    return normalize_candidates([RawCandidate(mask_png, mask_handle, "合并前景", None)], source,
        operation_id, granularity="combined_foreground", settings=settings)


def validate_mask_source(mask: MaskReference, source: ImageIdentity):
    if (mask.source_asset_id != source.asset_id or mask.source_sha256 != source.sha256
            or (mask.width, mask.height) != (source.width, source.height)):
        raise DetectionError("mask_source_mismatch")
