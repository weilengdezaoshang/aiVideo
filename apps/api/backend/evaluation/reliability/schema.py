"""Strict YAML: declarative data, no imports, expressions or executable hooks."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from backend.core.execution.fake import Scenario as ProviderBehavior
from backend.core.execution.contracts import WorkerSettings
from backend.core.events.contracts import EventSettings


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Fault(Contract):
    checkpoint: Literal[
        "none",
        "timer_scheduled",
        "wake",
        "planner",
        "plan",
        "provider_accepted",
        "t1",
        "t2",
        "publisher_sent",
    ] = "none"
    action: Literal["none", "kill_process"] = "none"

    @model_validator(mode="after")
    def paired(self):
        if (self.action == "none") != (self.checkpoint == "none"):
            raise ValueError("fault action and checkpoint must both be specified")
        return self


class Expected(Contract):
    run: Literal["completed", "failed", "cancelled"] = "completed"
    canvas_artifacts: int = Field(default=2, ge=0)
    charged: int = Field(default=6, ge=0)
    submissions: int | None = Field(default=None, ge=0)
    tasks: dict[str, str] = Field(default_factory=dict)


class Case(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    covers: list[str] = Field(min_length=1)
    probe: Literal[
        "none",
        "state_edges",
        "contracts",
        "event_integrity",
        "tool_contracts",
        "migration_cycle",
        "diagnostics_contract",
        "optimistic_lock",
        "transaction",
        "asset_versions",
        "typed_rules",
        "unknown_error",
        "event_order",
        "event_parallel",
        "dead_replay",
        "timer_restart",
        "timer_locked",
        "db_outage",
        "queue_backlog",
        "callbacks",
        "transfer_retry",
        "canvas_conflict",
        "cancel",
        "inflight_stop",
        "planner_rejected",
        "planner_limit",
        "planner_tokens",
        "publisher_crash",
        "mask",
    ] = "none"
    planner: list[dict] = Field(default_factory=list)
    provider: ProviderBehavior = Field(default_factory=lambda: ProviderBehavior(actual_credits=3))
    second_provider: ProviderBehavior | None = None
    fault: Fault = Field(default_factory=Fault)
    expect: Expected = Field(default_factory=Expected)
    mode: Literal["auto", "plan"] = "auto"
    message: str = "改图后生成视频"
    subjects: list[str] = Field(default_factory=lambda: ["白色杯子"])
    choice: str | None = None


class Suite(Contract):
    version: Literal[1]
    timeout_seconds: float = Field(default=300, gt=0, le=300)
    concurrency: int = Field(default=4, gt=0, le=8)
    case_timeout_seconds: float = Field(default=20, gt=0)
    max_steps: int = Field(default=100, gt=0)
    poll_seconds: float = Field(default=0.002, gt=0)
    event_count: int = Field(default=100, ge=100)
    parallel_runs: int = Field(default=10, ge=10)
    timer_restart_delay_seconds: float = Field(default=0.1, gt=0)
    timer_lock_seconds: float = Field(default=0.1, gt=0)
    initial_credits: int = Field(default=10000, gt=0)
    run_budget: int = Field(default=100, ge=0)
    token_budget: int = Field(default=100, ge=0)
    max_thought_rounds: int = Field(default=50, gt=0)
    quoted_credits: int = Field(default=10, ge=0)
    service_sla_seconds: float = Field(default=60, gt=0)
    worker: WorkerSettings = Field(
        default_factory=lambda: WorkerSettings(
            poll_seconds=0.001,
            retry_base_seconds=0.001,
            retry_max_seconds=0.01,
            retry_jitter=0,
            lease_seconds=0.05,
            heartbeat_seconds=0.01,
        )
    )
    events: EventSettings = Field(
        default_factory=lambda: EventSettings(
            concurrency=1,
            partitions=2,
            batch_size=200,
            prefetch=200,
            max_attempts=2,
            backoff_base_seconds=0.001,
            backoff_max_seconds=0.002,
            backoff_jitter=0,
            poll_seconds=0.01,
            publish_lease_seconds=0.03,
            timer_backoff_base_seconds=0.01,
            timer_backoff_max_seconds=0.02,
            timer_backoff_jitter=0,
            timer_skip_alert_threshold=2,
        )
    )
    scenarios: list[Case] = Field(min_length=20)

    @model_validator(mode="after")
    def unique(self):
        if len({c.id for c in self.scenarios}) != len(self.scenarios):
            raise ValueError("duplicate scenario id")
        return self
