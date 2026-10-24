"""Deterministic card-reference resolution. Program policy, no model calls.

Mirrors the supervisor prompt rule: demonstratives and card-kind words resolve
only against the visible card list; ambiguous or missing references ask the
user instead of guessing. The supervisor remains the final judge in LLM mode —
this module powers evaluation and rule-based planning.
"""
from __future__ import annotations

import re
from typing import Literal

from backend.core.contracts import Contract
from pydantic import Field

CardKind = Literal["subject", "style", "character", "environment"]

_KIND_WORDS: tuple[tuple[CardKind, tuple[str, ...]], ...] = (
    ("style", ("风格", "画风", "样式")),
    ("character", ("角色", "人物")),
    ("environment", ("环境", "场景", "背景")),
    ("subject", ("主体",)),
)
_AT_NAME = re.compile(r"[@＠]([^\s@＠，。,\.！!？?]{1,30})")
_DEMONSTRATIVE = re.compile(r"这(只|个|位|头|条|张)([\u4e00-\u9fa5A-Za-z0-9]{1,8})")
_BARE_REFERENCE = re.compile(r"这个|这张|那只|那只卡|选中的|选中")


class CardCandidate(Contract):
    card_id: str = Field(min_length=1)
    card_kind: CardKind
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    starred: bool = False
    selected: bool = False
    available: bool = True
    subject_category: str = Field(default="", max_length=64)


class CardReferenceResolution(Contract):
    status: Literal["resolved", "ambiguous", "unavailable"]
    card_id: str | None = None
    reason: str
    candidate_ids: list[str] = Field(default_factory=list)


def _noun_match(card: CardCandidate, noun: str) -> bool:
    """Reverse containment: the captured span usually runs past the noun itself."""
    noun = noun.strip()
    if not noun:
        return False
    if card.subject_category and card.subject_category in noun:
        return True
    if card.name and card.name in noun:
        return True
    if len(noun) <= 2 and (noun in card.name or noun in card.subject_category
                           or noun in card.description):
        return True
    return False


def resolve_card_reference(cards: list[CardCandidate], utterance: str) -> CardReferenceResolution:
    live = [card for card in cards if card.available]

    def resolved(card: CardCandidate, reason: str) -> CardReferenceResolution:
        return CardReferenceResolution(status="resolved", card_id=card.card_id, reason=reason,
                                       candidate_ids=[card.card_id])

    def ambiguous(reason: str, pool: list[CardCandidate]) -> CardReferenceResolution:
        return CardReferenceResolution(status="ambiguous", reason=reason,
                                       candidate_ids=[card.card_id for card in pool[:4]])

    def unavailable(reason: str) -> CardReferenceResolution:
        return CardReferenceResolution(status="unavailable", reason=reason)

    match = _AT_NAME.search(utterance)
    if match:
        name = match.group(1).strip()
        matched = [card for card in live if card.name == name]
        if len(matched) == 1:
            return resolved(matched[0], "at_name")
        if len(matched) > 1:
            return ambiguous("at_name_ambiguous", matched)
        return unavailable("at_name_not_found")

    for kind, words in _KIND_WORDS:
        if any(word in utterance for word in words):
            pool = [card for card in live if card.card_kind == kind]
            if not pool:
                return unavailable(f"no_{kind}_card")
            starred = [card for card in pool if card.starred]
            if len(pool) == 1:
                return resolved(pool[0], f"{kind}_kind")
            if len(starred) == 1:
                return resolved(starred[0], f"{kind}_starred")
            return ambiguous(f"{kind}_ambiguous", pool)

    match = _DEMONSTRATIVE.search(utterance)
    if match:
        noun = match.group(2)
        pool = [card for card in live
                if card.card_kind in ("subject", "character") and _noun_match(card, noun)]
        if len(pool) == 1:
            return resolved(pool[0], "subject_match")
        if len(pool) > 1:
            return ambiguous("subject_ambiguous", pool)
        selected = [card for card in live if card.selected]
        if len(selected) == 1:
            return resolved(selected[0], "selected")
        starred = [card for card in live if card.starred]
        if len(starred) == 1:
            return resolved(starred[0], "starred")
        return unavailable("no_matching_card")

    if _BARE_REFERENCE.search(utterance):
        selected = [card for card in live if card.selected]
        if len(selected) == 1:
            return resolved(selected[0], "selected")
        if len(selected) > 1:
            return ambiguous("multiple_selected", selected)
        starred = [card for card in live if card.starred]
        if len(starred) == 1:
            return resolved(starred[0], "starred")
        return unavailable("no_selection")

    return unavailable("no_reference_intent")
