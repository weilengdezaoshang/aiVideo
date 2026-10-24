"""Recognition contracts and tuning parameters. No tool or model imports here."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from backend.core.contracts import Contract
from pydantic_settings import BaseSettings, SettingsConfigDict


class RecognitionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_RECOGNITION_", frozen=True, extra="forbid")
    enabled: bool = True
    max_attempts: int = Field(default=2, ge=1, le=10)
    lease_seconds: float = Field(default=60, gt=0)
    scan_limit: int = Field(default=20, gt=0)
    max_image_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    retry_backoff_seconds: float = Field(default=30, gt=0)
    timeout_seconds: float = Field(default=45, gt=0)
    routes_file: str = ""
    route_id: str = ""


class SubjectRecognition(Contract):
    category: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=500)
    mask: dict | None = None  # Optional opaque mask reference; V1 analyzers return None.


class StyleRecognition(Contract):
    tags: list[str] = Field(default_factory=list, max_length=12)
    description: str = Field(default="", max_length=500)

    @field_validator("tags")
    @classmethod
    def bounded_tags(cls, value: list[str]) -> list[str]:
        for tag in value:
            if not isinstance(tag, str) or not tag.strip() or len(tag) > 32:
                raise ValueError("style tags must be short non-empty strings")
        return value


class RecognitionResult(Contract):
    subject: SubjectRecognition = Field(default_factory=SubjectRecognition)
    style: StyleRecognition = Field(default_factory=StyleRecognition)
    overall: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=128)


AnalysisPurpose = Literal["describe", "answer"]


class AnalyzerUnavailable(Exception):
    """No vision route is configured; callers skip or defer, never fail the main flow."""
