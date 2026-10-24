"""One serializable definition for planning, admission and worker configuration."""
from __future__ import annotations

from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MediaType = Literal["image", "video", "audio", "mask", "text"]
FailureType = Literal["retryable", "param_invalid", "no_subject", "blocked_by_model", "quota", "provider_down", "unknown"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False,
                              hide_input_in_errors=True)


class ToolSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_TOOLS_", frozen=True, extra="forbid")
    rollout_groups: dict[str, list[str]] = Field(default_factory=dict)
    max_count: int = Field(default=16, gt=0, le=16)
    min_frames: int = Field(default=2, ge=2, le=16)
    max_candidates: int = Field(default=64, gt=0)
    max_mask_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    max_image_pixels: int = Field(default=40_000_000, gt=0)
    recognition_timeout_seconds: float = Field(default=120, gt=0)
    local_timeout_seconds: float = Field(default=30, gt=0)
    export_timeout_seconds: float = Field(default=600, gt=0)
    max_clips: int = Field(default=200, gt=0, le=200)
    max_frames: int = Field(default=108000, gt=0, le=108000)
    # Local legacy endpoints do not charge credits. Explicit rates, never inferred provider prices.
    cutout_credits: int = Field(default=0, ge=0)
    export_credits: int | None = Field(default=None, ge=0)
    seed_minimum: int = Field(default=0, ge=0)
    seed_maximum: int = Field(default=2**31 - 1, ge=0)
    analyze_credits: int = Field(default=0, ge=0)
    analyze_timeout_seconds: float = Field(default=45, gt=0)
    analyze_max_image_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    max_card_refs: int = Field(default=2, gt=0, le=4)

    @model_validator(mode="after")
    def seed_bounds(self):
        if self.seed_minimum > self.seed_maximum:
            raise ValueError("seed range is inverted")
        return self


class Price(Contract):
    kind: Literal["fixed", "per_count", "unpriced"]
    credits: int | None = Field(default=None, ge=0)
    count_parameter: str | None = None

    @model_validator(mode="after")
    def valid_formula(self):
        if (self.kind == "unpriced") != (self.credits is None):
            raise ValueError("unpriced is the only formula without a rate")
        if (self.kind == "per_count") != (self.count_parameter is not None):
            raise ValueError("per_count requires a count parameter")
        return self


class Port(Contract):
    path: str = Field(min_length=1)
    media_type: MediaType
    required: bool = True
    # Output ports may exist only on one discriminated branch.
    when: dict[str, str] = Field(default_factory=dict)


class Execution(Contract):
    adapter: str
    binding: dict = Field(default_factory=dict)
    cancellable: bool = False
    cancellation: Literal["none", "cooperative", "provider"] = "none"
    provider_idempotent: bool = False
    result_url_ttl_seconds: int | None = Field(default=None, gt=0)
    result_url_policy: Literal["authenticated_asset", "transient", "provider_unspecified"]
    sla_seconds: float = Field(gt=0)
    sla_kind: Literal["timeout", "contract"] = "timeout"


class FailureRule(Contract):
    provider: str
    code: str
    failure: FailureType


class ToolDefinition(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1)
    parameters: dict
    result_schema: dict
    inputs: list[Port]
    outputs: list[Port]
    mode: Literal["generate", "deterministic", "form", "read_only"]
    price: Price
    limits: dict
    execution: Execution
    failures: list[FailureRule]
    rollout_groups: list[str] = Field(default_factory=list)
    available: bool = True
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def valid_definition(self):
        def check_secrets(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower().replace("_", "") in {"apikey", "secret", "password", "authorization", "accesstoken"}:
                        raise ValueError("tool definitions contain credential references only")
                    check_secrets(item)
            elif isinstance(value, list):
                for item in value:
                    check_secrets(item)
        check_secrets(self.execution.binding)
        Draft202012Validator.check_schema(self.parameters)
        Draft202012Validator.check_schema(self.result_schema)
        if self.parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object")
        if self.available and self.price.kind == "unpriced":
            raise ValueError("available tools require an explicit price")
        if not self.available and not self.unavailable_reason:
            raise ValueError("unavailable tools require a reason")
        if self.execution.cancellable != (self.execution.cancellation != "none"):
            raise ValueError("cancellation metadata disagrees")
        return self


class VersionLock(Contract):
    version: str
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class CatalogSnapshot(Contract):
    version: str = Field(min_length=1, max_length=128)
    schema_version: Literal[1] = 1
    tools: list[ToolDefinition]

    @model_validator(mode="after")
    def unique_names(self):
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("duplicate tool name")
        return self
