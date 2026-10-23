"""Program-owned execution contracts; no callable model tools."""
import math
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_EXECUTION_", frozen=True)
    lease_seconds: float = Field(default=30, gt=0)
    heartbeat_seconds: float = Field(default=10, gt=0)
    call_timeout_seconds: float = Field(default=60, gt=0)
    poll_seconds: float = Field(default=3, gt=0)
    retry_base_seconds: float = Field(default=1, gt=0)
    retry_max_seconds: float = Field(default=60, gt=0)
    retry_jitter: float = Field(default=0.2, ge=0, le=1)
    reconcile_seconds: float = Field(default=86400, gt=0)
    scan_limit: int = Field(default=100, gt=0)
    lock_timeout_ms: int = Field(default=100, gt=0)
    layout_gap: float = Field(default=40, ge=0)
    layout_steps: int = Field(default=1000, gt=0)
    default_width: float = Field(default=280, gt=0)
    default_height: float = Field(default=280, gt=0)
    probe_max_bytes: int = Field(default=65536, gt=0)
    export_width: int = Field(default=1280, ge=64, le=4096)
    export_height: int = Field(default=720, ge=64, le=4096)
    callback_max_bytes: int = Field(default=65536, gt=0)
    read_max_bytes: int = Field(default=16384, gt=0)
    ticket_key: str = Field(default="", repr=False)  # Fernet key supplied by explicit secret mount/env; never stored in snapshots.


@dataclass(frozen=True)
class ProviderResult:
    status: Literal["pending", "succeeded", "failed", "cancelled"]
    task_id: str | None
    locator: dict | None = None
    expires_at: float | None = None
    actual_credits: int = 0
    failure: str | None = None
    data: bytes | None = None
    output: dict | None = None
    ext: str = "png"
    submission_accepted: bool = True

    def __post_init__(self):
        if self.status not in {"pending", "succeeded", "failed", "cancelled"}:
            raise ValueError("invalid provider status")
        if self.task_id is not None and (not isinstance(self.task_id, str) or not 1 <= len(self.task_id) <= 256):
            raise ValueError("invalid provider task ID")
        if self.status == "pending" and self.task_id is None:
            raise ValueError("pending provider task requires an ID")
        if type(self.submission_accepted) is not bool:
            raise ValueError("invalid submission certainty")
        if type(self.actual_credits) is not int or self.actual_credits < 0:
            raise ValueError("invalid actual cost")
        if self.expires_at is not None and (type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at)):
            raise ValueError("invalid result expiry")
        if self.locator is not None and not isinstance(self.locator, dict):
            raise ValueError("invalid result locator")


class Provider(Protocol):
    idempotent: bool
    query_registration: bool
    cancellable: bool
    async def submit(self, registration_id: str, params: dict, idempotency_key: str | None) -> ProviderResult: ...
    async def query(self, task_id: str | None, registration_id: str) -> ProviderResult | None: ...
    async def download(self, locator: dict, kind: str) -> tuple[bytes, str]: ...
    async def cancel(self, task_id: str) -> ProviderResult: ...


async def before_call(task):
    """Return None by default. Rewrites require a fresh program-owned admission."""
    return None


async def after_result(task, asset):
    """Default no-op; implementations may enqueue events in the caller transaction."""
    return None
