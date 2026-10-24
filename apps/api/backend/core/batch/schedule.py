"""Bounded dispatch decisions. Production claims one workspace quota row; tests use an explicit ledger."""
from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import BatchSettings


@dataclass
class DispatchLedger:
    """Test double and pure decision log. The database claim path does not use this object."""

    limit: int
    inflight: int = 0
    rows: dict[str, dict] = field(default_factory=dict)

    def claim(self, key: str, *, credits: int, run_budget_left: int, run_inflight: int, per_run: int) -> str:
        current = self.rows.get(key)
        if current and current["state"] in {"reserved", "submitted", "settled"}:
            return "duplicate:" + current["state"]
        if credits > run_budget_left:
            self.rows[key] = {"state": "rejected_budget", "credits": credits}
            return "budget_rejected"
        if self.inflight >= self.limit or run_inflight >= per_run:
            return "concurrency_rejected"
        self.inflight += 1
        self.rows[key] = {"state": "reserved", "credits": credits}
        return "reserved"

    def ack(self, key: str) -> str:
        row = self.rows.get(key)
        if row is None or row["state"] == "rejected_budget":
            return "missing"
        if row["state"] == "submitted":
            return "duplicate:submitted"
        if row["state"] == "settled":
            return "duplicate:settled"
        row["state"] = "submitted"
        return "submitted"

    def settle(self, key: str) -> str:
        row = self.rows.get(key)
        if row is None or row["state"] != "submitted":
            return "not_submitted"
        row["state"] = "settled"
        self.inflight -= 1
        return "settled"

    def recover(self) -> list[str]:
        return [key for key, row in self.rows.items() if row["state"] == "reserved"]


def item_progress(document: dict, task_status: dict[str, str], task_errors: dict | None = None) -> dict:
    """``completedItems`` counts successful items only. Settled items include failures and skips."""

    terminal = {"succeeded", "failed", "blocked_by_model", "cancelled", "rejected"}
    errors = task_errors or {}
    completed = settled = failed = blocked = skipped = 0
    retryable: list[str] = []
    for item in document.get("items") or []:
        steps = item.get("steps") or []
        statuses = [task_status.get(step["task_key"], "pending") for step in steps]
        if not statuses or any(status not in terminal for status in statuses):
            continue
        settled += 1
        codes = [((errors.get(step["task_key"]) or {}).get("code")) for step in steps]
        if any(status == "blocked_by_model" for status in statuses):
            blocked += 1
        elif any(status == "failed" for status in statuses):
            failed += 1
            retryable.append(item["item_id"])
        elif all(status == "succeeded" for status in statuses):
            completed += 1
        elif "no_subject" in codes:
            skipped += 1
    total = int(document.get("item_count") or len(document.get("items") or []))
    return {"completedItems": completed, "settledItems": settled, "processedItems": settled,
            "totalItems": total, "failedCount": failed, "blockedCount": blocked, "skippedCount": skipped,
            "retryableItemIds": retryable, "settled": settled == total and total > 0}


def run_inflight(rows: list[dict], run_id: str) -> int:
    return sum(1 for row in rows if row.get("run_id") == run_id and row.get("state") in {"reserved", "submitted"})


def claim_decision(*, inflight: int, run_inflight: int, credits: int, budget_left: int,
                   existing: str | None, settings: BatchSettings | None = None) -> str:
    """Global and per-run caps count slots that are still reserved or submitted.

    ``max_per_batch`` is how many tasks one ``advance`` may newly queue. It is not a
    lifetime total and is not applied here, so settled slots can be reused.
    """

    settings = settings or BatchSettings()
    if existing in {"reserved", "submitted", "settled"}:
        return "duplicate:" + existing
    if credits > budget_left:
        return "budget_rejected"
    if inflight >= settings.global_concurrency or run_inflight >= settings.max_per_run:
        return "concurrency_rejected"
    return "reserve"
