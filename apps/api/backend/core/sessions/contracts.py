"""Configurable session conversation thresholds. Values live here, not in call sites."""
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.core.contracts import Contract


class SessionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_SESSION_", frozen=True)
    quiet_window_seconds: float = Field(default=1.2, gt=0)
    max_merge_wait_seconds: float = Field(default=4.0, gt=0)
    queue_limit: int = Field(default=100, gt=0)
    scan_batch: int = Field(default=50, gt=0)
    dispatch_lease_seconds: float = Field(default=15, gt=0)
    heartbeat_seconds: float = Field(default=30, gt=0)
    retry_seconds: float = Field(default=1, gt=0)
    summary_max_chars: int = Field(default=4000, gt=0)
    summary_recent_messages: int = Field(default=20, gt=0)
    artifact_candidate_limit: int = Field(default=32, gt=0)
    sse_replay_retain: int = Field(default=10000, gt=0)
    sse_replay_batch: int = Field(default=200, gt=0)
    slow_task_notify_seconds: float = Field(default=60, ge=0)
    seed_min: int = Field(default=0, ge=0)
    seed_max: int = Field(default=2_147_483_647, gt=0)
    max_message_chars: int = Field(default=8000, gt=0)


def merge_deadline(now: datetime, first_created: datetime, settings: SessionSettings) -> datetime:
    quiet = now + timedelta(seconds=settings.quiet_window_seconds)
    cap = first_created + timedelta(seconds=settings.max_merge_wait_seconds)
    return min(quiet, cap)


class EnqueueResult(Contract):
    message_id: UUID
    sequence: int
    dispatch_at: datetime | None = None
    run_id: UUID | None = None
    replayed: bool = False
