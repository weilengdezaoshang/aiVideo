"""Creation contracts and configurable defaults; references have no executable tools."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_CORE_", frozen=True)
    credits_limit: int = Field(default=0, ge=0)
    tokens_limit: int = Field(default=65536, ge=0)
    max_thought_rounds: int = Field(default=20, gt=0)
    max_task_retries: int = Field(default=2, ge=0)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunInput(Contract):
    session_id: UUID
    workspace_id: UUID
    user_id: str
    request_key: str
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot: dict
    scope: dict
    invariants: dict
    version_lock: dict
    mode: Literal["auto", "plan"] = "auto"
    parent_run_id: UUID | None = None
    parent_task_id: UUID | None = None
    input_revision: int = Field(default=1, gt=0)
    correction_of_run_id: UUID | None = None


class TaskInput(Contract):
    run_id: UUID
    plan_id: UUID
    plan_version: int = Field(gt=0)
    task_key: str
    kind: Literal["call_tool", "spawn_helper", "ask_user", "wait_user"]
    idempotency_key: str
    parameters: dict
    output_refs: list = Field(default_factory=list)
    scope: dict
    invariants: dict = Field(default_factory=dict)
    tier: Literal["draft", "final"] = "draft"
    tool_ref: str | None = None
    tool_version: str | None = None
    capability_ref: str | None = None
    capability_version: str | None = None
    skill_version_id: UUID | None = None
    stage_key: str | None = None
    milestone_key: str | None = None
    input_revision: int = Field(default=1, gt=0)


class AssetContent(Contract):
    kind: Literal["image", "video", "audio", "mask", "text"]
    category: Literal["artifact", "card"]
    card_kind: Literal["subject", "style", "character", "environment"] | None = None
    starred: bool = False
    recognition: dict = Field(default_factory=dict)
    layers: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)
    storage: dict
    stored_asset_id: UUID | None = None
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    status: Literal["normal", "reported", "removed"] = "normal"
    run_id: UUID | None = None
    task_id: UUID | None = None
    user_id: str
