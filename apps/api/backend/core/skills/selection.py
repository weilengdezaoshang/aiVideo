"""Skill selection: explicit choice > scene rule match > BM25 retrieval. Pure core, DB shell.

The pure core takes plain skill views so the evaluation harness can drive the
same priority chain without a database.
"""
from __future__ import annotations

import math
from collections import Counter

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.core.contracts import Contract

from .contracts import SkillSpec


class SkillSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_SKILLS_", frozen=True, extra="forbid")
    rule_threshold: float = Field(default=0.34, gt=0, le=1)
    retrieval_threshold: float = Field(default=1.2, gt=0)
    retrieval_candidates: int = Field(default=3, gt=0)
    context_body_chars: int = Field(default=4000, gt=0)


class SkillView(Contract):
    """Retrieval-facing projection of one skill version."""

    skill_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    scenes: list[str] = Field(min_length=1)
    allowlist: list[str] = Field(min_length=1)
    spec_version: str = Field(default="1")
    source: str = Field(default="official")

    @classmethod
    def of(cls, skill_id, version_id, spec: SkillSpec) -> "SkillView":
        return cls(skill_id=str(skill_id), version_id=str(version_id), name=spec.name,
            scenes=list(spec.scenes), allowlist=list(spec.allowlist),
            spec_version=spec.version, source=spec.source)


def _grams(text: str) -> set[str]:
    """Character bigrams keep Chinese scene matching tokenizer-free."""
    cleaned = "".join(text.split())
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)} if len(cleaned) > 1 else {cleaned}


def rule_score(skill: SkillView, message: str) -> float:
    """Share of a skill's scene bigrams present in the user message."""
    message_grams = _grams(message)
    best = 0.0
    for scene in skill.scenes:
        grams = _grams(scene)
        if not grams:
            continue
        best = max(best, len(grams & message_grams) / len(grams))
    return best


def _bm25(message: str, skills: list[SkillView], settings: SkillSettings) -> list[tuple[float, SkillView]]:
    scored = []
    docs = []
    for skill in skills:
        grams = Counter()
        for token in _grams(skill.name):
            grams[token] += 2
        for scene in skill.scenes:
            for token in _grams(scene):
                grams[token] += 1
        docs.append(grams)
    query = _grams(message)
    total = len(docs)
    for index, skill in enumerate(skills):
        grams = docs[index]
        if not grams:
            continue
        score = 0.0
        length = sum(grams.values())
        average = max(sum(sum(doc.values()) for doc in docs) / total, 1)
        for token in query:
            if token not in grams:
                continue
            df = sum(1 for doc in docs if token in doc)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            score += idf * grams[token] * 3 / (grams[token] + 2 * (1 - 0.75 + 0.75 * length / average))
        scored.append((score, skill))
    return sorted(scored, key=lambda item: (-item[0], item[1].skill_id))[:settings.retrieval_candidates]


def match_skill(skills: list[SkillView], message: str, *, explicit: str | None = None,
                settings: SkillSettings | None = None) -> SkillView | None:
    """Priority chain; no skill keeps today's unrestricted behavior."""
    settings = settings or SkillSettings()
    if explicit:
        wanted = explicit.strip()
        for skill in skills:
            if wanted in {skill.skill_id, skill.name}:
                return skill
        return None
    ruled = [(rule_score(skill, message), skill) for skill in skills]
    top_rule = max(ruled, key=lambda item: (item[0], item[1].skill_id), default=None)
    if top_rule and top_rule[0] >= settings.rule_threshold:
        return top_rule[1]
    retrieved = _bm25(message, skills, settings)
    if retrieved and retrieved[0][0] >= settings.retrieval_threshold:
        return retrieved[0][1]
    return None


async def visible_skills(tx, *, workspace_id, user_id) -> list[tuple[SkillView, SkillSpec]]:
    """Official playbooks plus this user's saved skills in the workspace."""
    from sqlalchemy import or_, select

    from backend.core.orm import CoreSkill, CoreSkillVersion
    from .store import spec_of

    rows = (await tx.scalars(select(CoreSkill).where(or_(CoreSkill.workspace_id == workspace_id,
        CoreSkill.workspace_id.is_(None)), or_(CoreSkill.user_id == user_id, CoreSkill.user_id.is_(None)))))
    views = []
    for skill in rows:
        if skill.current_version_id is None:
            continue
        version = await tx.get(CoreSkillVersion, skill.current_version_id)
        if version is None:
            continue
        views.append((SkillView.of(skill.id, version.id, await spec_of(tx, version)),
                      await spec_of(tx, version)))
    return views


async def select_skill(tx, *, workspace_id, user_id, message: str,
                       explicit_skill_id: str | None = None,
                       settings: SkillSettings | None = None) -> tuple[SkillView, SkillSpec] | None:
    candidates = await visible_skills(tx, workspace_id=workspace_id, user_id=user_id)
    picked = match_skill([view for view, _ in candidates], message,
        explicit=explicit_skill_id, settings=settings)
    if picked is None:
        return None
    return next(((view, spec) for view, spec in candidates if view.skill_id == picked.skill_id), None)
