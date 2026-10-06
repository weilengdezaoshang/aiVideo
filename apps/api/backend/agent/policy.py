"""Governance of a Run's tool use: budgets, duplicate suppression, circuit breaking.

Decisions are pure functions of the stored rows so a recovered Run reaches the same
verdicts. A decision only ever *restricts* what the model may trigger.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from pydantic import ValidationError

from backend.agent import evidence as ev

MAX_FORMAT_REPAIRS = 2
MAX_ANSWER_REVISIONS = 2
CIRCUIT_AFTER = 2          # identical failures of one tool before it is refused
FAIL_AFTER = 3             # consecutive failures of one tool that end the Run


@dataclass(frozen=True)
class Run:
    """Execute the call."""


@dataclass(frozen=True)
class Duplicate:
    of: str
    next: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Reject:
    code: str
    message: str


@dataclass(frozen=True)
class Fail:
    code: str
    message: str


Admission = Run | Duplicate | Reject | Fail


class Policy:
    def __init__(self, gateway):
        self.gateway = gateway

    def key(self, name: str, arguments: dict) -> str:
        """Stable identity of a call; defaults are filled in so `{}` equals explicit defaults."""
        spec = self.gateway.specs.get(name)
        canonical: Any = arguments
        if spec is not None:
            try:
                canonical = spec.input.model_validate(arguments).model_dump(by_alias=True, mode="json")
            except ValidationError:
                canonical = arguments
        raw = json.dumps([name, canonical], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def executed_keys(self, rows: list[dict]) -> set[str]:
        return {self.key(row["name"], row["arguments"]) for row in rows
                if isinstance(row.get("output"), dict) and row["output"].get("status") == "ok"}

    def admit_read(self, rows: list[dict], name: str, arguments: dict, batch_ok: dict[str, int]) -> Admission:
        """Decide one read call. ``batch_ok`` maps keys of earlier calls in this batch to their ordinal."""
        key = self.key(name, arguments)
        if key in batch_ok:
            return Duplicate(ev.evidence_id(batch_ok[key]))
        for row in reversed(rows):
            output = row.get("output")
            if row["name"] == name and isinstance(output, dict) and output.get("status") == "ok" \
                    and self.key(name, row["arguments"]) == key:
                executed = self.executed_keys(rows)
                carried = tuple(item for item in output["result"]["next"]
                                if not self._done(item, executed))
                return Duplicate(output["evidenceId"], carried)
        streak = self._failure_streak(rows, name)
        if streak >= FAIL_AFTER:
            return Fail("REPEATED_FAILURE", f"工具 {name} 连续失败 {streak} 次，已停止运行")
        if streak >= CIRCUIT_AFTER:
            return Reject("CIRCUIT_OPEN", f"工具 {name} 已连续失败 {streak} 次，请换一种做法或说明无法完成")
        return Run()

    def _done(self, item: dict, executed: set[str]) -> bool:
        return item["action"] == "call_tool" and item["arguments"] is not None \
            and self.key(item["tool"], item["arguments"]) in executed

    @staticmethod
    def _failure_streak(rows: list[dict], name: str) -> int:
        """Consecutive trailing failures of ``name``; any other read call or a success resets it."""
        streak = 0
        for row in reversed([r for r in rows if r.get("kind") == "read"]):
            if row["name"] != name or row["status"] != "failed":
                break
            streak += 1
        return streak

    def filter_next(self, envelope: dict, rows: list[dict]) -> dict:
        """Remove suggestions that were already executed in this Run."""
        executed = self.executed_keys(rows)
        if envelope.get("status") == "ok":
            result = envelope["result"]
            result["next"] = [item for item in result["next"] if not self._done(item, executed)]
        elif envelope.get("status") == "error":
            error = envelope["error"]
            error["next"] = [item for item in error["next"] if not self._done(item, executed)]
        return envelope
