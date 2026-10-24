"""Skill persistence: content-hash idempotent upserts over the version chain."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from backend.core.orm import CoreSkill, CoreSkillVersion
from backend.core.states import EntityNotFound

from .contracts import SkillRouting, SkillSpec, parse_skill_markdown

OFFICIAL_DIR_NAME = "official"


async def find_skill(tx, *, name: str, workspace_id: uuid.UUID | None, user_id: str | None) -> CoreSkill | None:
    """Official skills live outside any workspace; user skills are workspace+user scoped."""
    query = select(CoreSkill).where(CoreSkill.name == name)
    if workspace_id is None:
        query = query.where(CoreSkill.workspace_id.is_(None), CoreSkill.user_id.is_(None))
    else:
        query = query.where(CoreSkill.workspace_id == workspace_id, CoreSkill.user_id == user_id)
    return await tx.scalar(query.limit(1))


async def upsert_skill(tx, spec: SkillSpec, *, workspace_id: uuid.UUID | None = None,
                       user_id: str | None = None, source_ref: str | None = None) -> tuple[CoreSkill, CoreSkillVersion]:
    """Same content hash is a no-op; new content appends a version and moves the head."""
    digest = spec.content_hash()
    skill = await find_skill(tx, name=spec.name, workspace_id=workspace_id, user_id=user_id)
    if skill is not None and skill.current_version_id is not None:
        head = await tx.get(CoreSkillVersion, skill.current_version_id)
        if head is not None and head.content_hash == digest:
            return skill, head
    if skill is None:
        skill = CoreSkill(workspace_id=workspace_id, user_id=user_id, name=spec.name)
        tx.add(skill)
        await tx.flush()
    head_number = 0
    if skill.current_version_id is not None:
        head = await tx.get(CoreSkillVersion, skill.current_version_id)
        head_number = head.version if head else 0
    version = CoreSkillVersion(skill_id=skill.id, version=head_number + 1,
        scenario="\n".join(spec.scenes), tool_allowlist=list(spec.allowlist),
        phases=[phase.model_dump(mode="json") for phase in spec.phases],
        confirmation_points=[{"phase": phase.name} for phase in spec.phases if phase.confirm],
        boundaries={"items": spec.boundaries,
                    "routing": spec.routing.model_dump(mode="json"),
                    "defaults": spec.defaults, "spec_version": spec.version, "role": spec.role},
        source=spec.source, source_ref=source_ref, content_hash=digest, body=spec.body)
    tx.add(version)
    await tx.flush()
    skill.current_version_id = version.id
    await tx.flush()
    return skill, version


async def spec_of(tx, version: CoreSkillVersion) -> SkillSpec:
    """Rebuild the spec from a stored version (context injection and rule compilation)."""
    skill = await tx.get(CoreSkill, version.skill_id)
    if skill is None:
        raise EntityNotFound("skill_not_found")
    boundaries = version.boundaries or {}
    return SkillSpec(name=skill.name, version=str(boundaries.get("spec_version") or str(version.version)),
        scenes=[line for line in (version.scenario or "").splitlines() if line.strip()],
        role=str(boundaries.get("role") or ""), body=version.body or "",
        allowlist=list(version.tool_allowlist or []),
        phases=[{"name": phase.get("name", ""), "tools": list(phase.get("tools") or []),
                 "required": bool(phase.get("required", True)), "confirm": bool(phase.get("confirm", False))}
                for phase in (version.phases or [])],
        routing=SkillRouting.model_validate(boundaries.get("routing") or {}),
        boundaries=list(boundaries.get("items") or []),
        defaults=dict(boundaries.get("defaults") or {}), source=version.source)


def seed_official_skills() -> list[tuple[str, SkillSpec]]:
    """Parse every bundled official playbook; file errors fail loudly at startup."""
    from pathlib import Path
    directory = Path(__file__).resolve().parent / OFFICIAL_DIR_NAME
    parsed = []
    for path in sorted(directory.glob("*.md")):
        spec = parse_skill_markdown(path.read_text(encoding="utf-8"))
        if spec.source != "official":
            raise ValueError(f"official skill file must declare source=official: {path.name}")
        parsed.append((path.name, spec))
    return parsed


async def seed_official(tx) -> int:
    """Idempotent startup seeding; a reseed with unchanged files touches nothing."""
    changed = 0
    for filename, spec in seed_official_skills():
        await upsert_skill(tx, spec, workspace_id=None, user_id=None, source_ref=filename)
        changed += 1
    return changed
