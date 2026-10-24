"""Deterministic summary fallback. Original messages and confirmed decisions remain in DB."""

import hashlib
import json
from dataclasses import dataclass
from typing import Sequence

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SummarySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_SESSION_SUMMARY_")
    threshold_chars: int = Field(default=12000, gt=0)
    keep_recent_messages: int = Field(default=12, ge=1)
    excerpt_chars: int = Field(default=240, gt=0)
    max_summary_chars: int = Field(default=4000, gt=0)


@dataclass(frozen=True)
class SummaryInput:
    sequence: int
    role: str
    text: str


@dataclass(frozen=True)
class SummaryResult:
    cursor: int
    text: str
    input_hash: str
    decision_ids: tuple[str, ...]


def compact(
    messages: Sequence[SummaryInput], *, previous_cursor: int,
    previous_summary: str, confirmed_decisions: dict[str, str],
    settings: SummarySettings | None = None,
) -> SummaryResult | None:
    """Append bounded excerpts without accepting summary text as instructions or decisions.

    Caller persists the result before a cursor CAS. Decisions are separate references,
    always reloaded verbatim when building context; truncation never edits that ledger.
    """
    settings = settings or SummarySettings()
    rows = sorted((row for row in messages if row.sequence > previous_cursor), key=lambda row: row.sequence)
    if len({row.sequence for row in rows}) != len(rows):
        raise ValueError("duplicate_summary_sequence")
    if sum(len(row.text) for row in rows) < settings.threshold_chars:
        return None
    old = rows[:-settings.keep_recent_messages]
    if not old:
        return None
    canonical = json.dumps({
        "previous_cursor": previous_cursor, "previous_summary": previous_summary,
        "messages": [{"sequence": row.sequence, "role": row.role, "text": row.text} for row in old],
        "decisions": confirmed_decisions,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # Keep recent excerpts bounded; DB originals remain the full source of truth.
    excerpts = [f"[{row.sequence}:{row.role}] {row.text[:settings.excerpt_chars]}" for row in old]
    text = "\n".join(filter(None, [previous_summary, *excerpts]))[-settings.max_summary_chars:]
    return SummaryResult(old[-1].sequence, text, hashlib.sha256(canonical.encode()).hexdigest(),
                         tuple(sorted(confirmed_decisions)))
