"""Shared planner contract and all supervisor tuning parameters."""
from typing import Literal, Protocol
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from backend.core.contracts import Contract


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_SUPERVISOR_", frozen=True)
    review_retries: int = Field(default=2, ge=0)
    confidence_threshold: float = Field(default=0.8, ge=0, le=1)
    lease_seconds: float = Field(default=30, gt=0)
    planner_timeout_seconds: float = Field(default=10, gt=0)
    concurrency: int = Field(default=4, gt=0)
    scan_limit: int = Field(default=100, gt=0)
    context_messages: int = Field(default=30, gt=0)
    max_tasks: int = Field(default=100, gt=0)
    lock_timeout_ms: int = Field(default=100, gt=0)
    retry_seconds: float = Field(default=1, gt=0)
    planner_failures: int = Field(default=3, gt=0)


class Step(Contract):
    id: str = Field(pattern=r"^t[1-9][0-9]*$")
    kind: Literal["call_tool", "ask_user"] = "call_tool"
    tool: str | None = None
    parameters: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    placement: dict = Field(default_factory=dict)


class Plan(Contract):
    goal: str
    steps: list[Step] = Field(min_length=1)


class PlannerResult(Contract):
    plan: Plan
    invariants: dict = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    raw_output: dict
    tokens: int = Field(default=0, ge=0)


class Planner(Protocol):
    async def plan(self, context: dict) -> PlannerResult: ...
