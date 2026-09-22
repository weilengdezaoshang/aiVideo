"""Provider operation budgets and errors. No implicit retries or billable probes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import TypeVar

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.errors import AppError, ErrorCode, Recovery

T = TypeVar("T")


class Operation(str, Enum):
    PREPARE = "prepare"
    SUBMIT = "submit"
    POLL = "poll"
    DOWNLOAD = "download"
    CANCEL = "cancel"
    RECONCILE = "reconcile"
    METADATA = "metadata"


class Certainty(str, Enum):
    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


class ProviderPolicy(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_POLICY_", frozen=True)

    pool_s: float = Field(default=2, gt=0)
    connect_s: float = Field(default=5, gt=0)
    write_s: float = Field(default=30, gt=0)
    submit_s: float = Field(default=30, gt=0)
    poll_s: float = Field(default=30, gt=0)
    upload_s: float = Field(default=120, gt=0)
    image_download_s: float = Field(default=60, gt=0)
    video_download_s: float = Field(default=120, gt=0)
    download_recovery_s: float = Field(default=300, gt=0)
    reconcile_s: float = Field(default=86400, gt=0)
    queue_s: float = Field(default=1800, gt=0)
    export_s: float = Field(default=1800, gt=0)
    lease_s: float = Field(default=90, ge=30)
    heartbeat_s: float = Field(default=15, gt=0, le=20)
    max_retries: int = Field(default=3, ge=0, le=10)
    global_capacity: int = Field(default=200, ge=1)
    outbox_capacity: int = Field(default=10000, ge=1)
    outbox_max_attempts: int = Field(default=20, ge=1)
    workspace_capacity: int = Field(default=20, ge=1)
    user_per_minute: int = Field(default=10, ge=1)
    cloud_capacity: int = Field(default=2, ge=1)
    comfy_capacity: int = Field(default=1, ge=1)
    provider_submit_per_minute: int = Field(default=60, ge=1)
    provider_poll_per_minute: int = Field(default=120, ge=1)
    provider_download_per_minute: int = Field(default=30, ge=1)
    breaker_window_s: int = Field(default=30, ge=1)
    breaker_min_calls: int = Field(default=10, ge=1)
    breaker_threshold: float = Field(default=0.5, gt=0, le=1)
    breaker_cooldown_s: int = Field(default=30, ge=1)
    breaker_recovery_s: int = Field(default=30, ge=1)

    def http_timeout(self, read_s: float) -> httpx.Timeout:
        return httpx.Timeout(connect=self.connect_s, pool=self.pool_s,
                             write=self.write_s, read=read_s)

    def operation_seconds(self, operation: Operation, *, video: bool = False) -> float:
        if operation == Operation.PREPARE:
            return self.upload_s
        if operation == Operation.DOWNLOAD:
            return self.video_download_s if video else self.image_download_s
        return self.submit_s if operation == Operation.SUBMIT else self.poll_s


class ProviderFailure(AppError):
    """Safe public message plus machine-readable submission/retry semantics."""

    def __init__(self, code: ErrorCode, *, operation: Operation,
                 certainty: Certainty = Certainty.REJECTED, retryable: bool = False,
                 availability_failure: bool = False, retry_after: float | None = None):
        self.code = code
        self.operation = operation
        self.certainty = certainty
        self.retryable = retryable
        self.availability_failure = availability_failure
        self.retry_after = retry_after
        self.http_status = 429 if code == ErrorCode.UPSTREAM_RATE_LIMIT else 502
        self.recovery = Recovery.RETRY if retryable else Recovery.CONTACT
        if operation == Operation.SUBMIT and certainty == Certainty.UNKNOWN:
            self.code = ErrorCode.UPSTREAM_UNKNOWN
            self.recovery = Recovery.RECONCILE
            self.retryable = False
        super().__init__({
            ErrorCode.UPSTREAM_UNKNOWN: "生成请求结果待确认，请勿重复提交",
            ErrorCode.UPSTREAM_TIMEOUT: "上游操作超时",
            ErrorCode.UPSTREAM_RATE_LIMIT: "上游配额暂时受限",
            ErrorCode.UPSTREAM_AUTH: "上游凭据不可用，请检查配置",
            ErrorCode.INVALID_PARAM: "上游拒绝请求，请检查参数",
        }.get(self.code, "上游服务暂时不可用"))


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        if not 0 <= seconds < float("inf"):
            return None
        return seconds
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, (date - (now or datetime.now(timezone.utc))).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def classify_failure(exc: Exception, operation: Operation) -> ProviderFailure:
    if isinstance(exc, ProviderFailure):
        return exc
    if isinstance(exc, httpx.PoolTimeout):
        return ProviderFailure(ErrorCode.UPSTREAM_UNAVAILABLE, operation=operation,
                               certainty=Certainty.NOT_SENT, retryable=True)
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
        return ProviderFailure(ErrorCode.UPSTREAM_UNAVAILABLE, operation=operation,
                               certainty=Certainty.NOT_SENT, retryable=True,
                               availability_failure=True)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return ProviderFailure(ErrorCode.UPSTREAM_AUTH, operation=operation)
        if status == 429:
            return ProviderFailure(ErrorCode.UPSTREAM_RATE_LIMIT, operation=operation,
                                   retryable=True,
                                   retry_after=retry_after_seconds(exc.response.headers.get("Retry-After")))
        if status >= 500:
            return ProviderFailure(ErrorCode.UPSTREAM_UNAVAILABLE, operation=operation,
                                   certainty=Certainty.UNKNOWN if operation == Operation.SUBMIT else Certainty.REJECTED,
                                   retryable=True, availability_failure=True)
        return ProviderFailure(ErrorCode.INVALID_PARAM, operation=operation)
    timeout = isinstance(exc, (TimeoutError, httpx.TimeoutException))
    return ProviderFailure(ErrorCode.UPSTREAM_TIMEOUT if timeout else ErrorCode.UPSTREAM_UNAVAILABLE,
                           operation=operation, certainty=Certainty.UNKNOWN,
                           retryable=operation != Operation.SUBMIT,
                           availability_failure=True)


def remaining_seconds(deadline: datetime, now: datetime | None = None) -> float:
    if deadline.tzinfo is None:
        raise ValueError("deadline must be timezone-aware")
    return max(0, (deadline - (now or datetime.now(timezone.utc))).total_seconds())


async def bounded_call(operation: Operation, deadline: datetime, budget_s: float,
                       call: Callable[[], Awaitable[T]]) -> T:
    budget = min(budget_s, remaining_seconds(deadline))
    if budget <= 0:
        raise ProviderFailure(ErrorCode.UPSTREAM_TIMEOUT, operation=operation,
                              certainty=Certainty.NOT_SENT)
    try:
        async with asyncio.timeout(budget):
            return await call()
    except ProviderFailure:
        raise
    except (TimeoutError, httpx.HTTPError) as exc:
        raise classify_failure(exc, operation) from exc
