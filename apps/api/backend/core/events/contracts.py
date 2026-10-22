from enum import StrEnum
import hashlib
import math
import random
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EventType(StrEnum):
    USER_MESSAGE = "user_message"
    USER_INTERRUPT = "user_interrupt"
    PLAN_WRITTEN = "plan_written"
    PLAN_REJECTED = "plan_rejected"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"
    TASK_BLOCKED = "task_blocked"
    HELPER_REPORT = "helper_report"
    HELPER_HELP = "helper_help"
    HELPER_INSTRUCTION = "helper_instruction"
    CONFLICT = "conflict"
    ASSET_CHANGED = "asset_changed"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    USER_CONFIRMED = "user_confirmed"
    USER_CANCELLED = "user_cancelled"
    RUN_PAUSED = "run_paused"
    RUN_RESUMED = "run_resumed"


class EventSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_CORE_EVENTS_", frozen=True)
    partitions: int = Field(default=32, gt=0)
    concurrency: int = Field(default=16, gt=0)
    batch_size: int = Field(default=100, gt=0)
    prefetch: int = Field(default=32, gt=0)
    publish_lease_seconds: float = Field(default=60, gt=0)
    delivery_visibility_seconds: float = Field(default=120, gt=0)
    processing_lease_seconds: float = Field(default=60, gt=0)
    handler_timeout_seconds: float = Field(default=30, gt=0)
    statement_timeout_ms: int = Field(default=30000, gt=0)
    max_attempts: int = Field(default=5, gt=0)
    backoff_base_seconds: float = Field(default=1, gt=0)
    backoff_max_seconds: float = Field(default=300, gt=0)
    backoff_jitter: float = Field(default=0.2, ge=0, le=1)
    poll_seconds: float = Field(default=1, gt=0)
    missing_handler_poll_seconds: float = Field(default=10, gt=0)
    broker_timeout_seconds: float = Field(default=10, gt=0)
    publish_alert_attempts: int = Field(default=8, gt=0)
    backlog_alert_seconds: float = Field(default=120, gt=0)
    error_max_chars: int = Field(default=2000, gt=0)
    causal_max_depth: int = Field(default=256, gt=0)
    admin_lock_timeout_ms: int = Field(default=1000, gt=0)
    lock_timeout_ms: int = Field(default=5, gt=0)
    lock_recovery_attempts: int = Field(default=5, gt=0)
    lock_recovery_delay_seconds: float = Field(default=0.01, gt=0)
    timer_lease_seconds: float = Field(default=60, gt=0)
    timer_backoff_base_seconds: float = Field(default=0.1, gt=0)
    timer_backoff_max_seconds: float = Field(default=5, gt=0)
    timer_backoff_jitter: float = Field(default=0.2, ge=0, lt=1)
    timer_skip_alert_threshold: int = Field(default=20, gt=0)
    timer_accuracy_seconds: float = Field(default=1, gt=0)
    handler_modules: list[str] = Field(default_factory=lambda: ["backend.core.supervisor.engine"])

    @model_validator(mode="after")
    def consistent(self):
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff maximum must cover base")
        if self.timer_backoff_max_seconds < self.timer_backoff_base_seconds:
            raise ValueError("timer backoff maximum must cover base")
        return self

    def delay(self, attempts: int) -> float:
        exponent = min(max(attempts - 1, 0), math.ceil(math.log2(self.backoff_max_seconds / self.backoff_base_seconds)))
        delay = min(self.backoff_max_seconds, self.backoff_base_seconds * 2 ** exponent)
        return delay * (1 - random.uniform(0, self.backoff_jitter))


class ClaimCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: UUID
    run_id: UUID


def partition(run_id: UUID, count: int) -> int:
    if count <= 0:
        raise ValueError("partition count must be positive")
    return int.from_bytes(hashlib.sha256(run_id.bytes).digest()[:8], "big") % count


def timer_delay(settings, skips):
    exponent = min(max(skips - 1, 0), math.ceil(math.log2(
        settings.timer_backoff_max_seconds / settings.timer_backoff_base_seconds)))
    return min(settings.timer_backoff_max_seconds,
        settings.timer_backoff_base_seconds * 2 ** exponent) * (1 - random.uniform(0, settings.timer_backoff_jitter))
