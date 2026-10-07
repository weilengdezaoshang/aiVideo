"""Immutable view of a Run that the orchestrator reasons about (loaded by the loop)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StepState:
    id: str
    key: str
    ordinal: int
    title: str
    intent: str
    tools: tuple[str, ...]
    depends_on: tuple[str, ...]
    done_when: str
    status: str                    # pending | running | completed | blocked
    summary: str | None = None
    evidence_refs: tuple[str, ...] = ()
    retries: int = 0


@dataclass(frozen=True)
class State:
    stage: str                     # plan | execute | respond | reflect
    snapshot: dict
    message: str
    counters: dict
    rows: tuple[dict, ...]         # tool rows in order, each with step_id
    tool_call_count: int
    model_call_count: int = 0
    plan: dict | None = None       # {"goal", "version", "current"}
    steps: tuple[StepState, ...] = ()
    draft: dict | None = None      # claims payload of the last verified finish
    waiting: dict | None = None
    reflections: tuple[dict, ...] = field(default=())

    @property
    def revision(self) -> int:
        return self.snapshot["revision"]

    @property
    def current(self) -> StepState | None:
        key = (self.plan or {}).get("current")
        return next((step for step in self.steps if step.key == key), None)

    def step_by_key(self, key: str) -> StepState | None:
        return next((step for step in self.steps if step.key == key), None)

    @property
    def read_rows(self) -> list[dict]:
        return [row for row in self.rows if row.get("kind") == "read"]
