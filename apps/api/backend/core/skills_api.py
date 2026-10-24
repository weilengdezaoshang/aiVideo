"""Skill HTTP surface: list playbooks, draft one from a successful run, save mine."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from backend.core.orm import CoreSkill, CoreSkillVersion
from backend.core.skills.contracts import SkillSpec
from backend.core.skills.draft import draft_from_run
from backend.core.skills.store import spec_of, upsert_skill
from backend.core.states import EntityNotFound
from backend.services.identity import Identity, authenticated


class SaveSkillBody(BaseModel):
    spec: dict = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)


async def _visible_version(tx, skill_id: uuid.UUID, identity: Identity) -> CoreSkillVersion | None:
    row = (await tx.execute(select(CoreSkill, CoreSkillVersion)
        .join(CoreSkillVersion, CoreSkillVersion.id == CoreSkill.current_version_id)
        .where(CoreSkill.id == skill_id,
            or_(CoreSkill.workspace_id == identity.workspace_id, CoreSkill.workspace_id.is_(None)),
            or_(CoreSkill.user_id == identity.principal, CoreSkill.user_id.is_(None)))
        .limit(1))).first()
    return (row[1] if row else None), (row[0] if row else None)


def build_skills_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/core/skills")
    async def listing(request: Request, identity: Identity = Depends(authenticated)):
        factory = request.app.state.factory
        async with factory() as tx:
            rows = (await tx.execute(select(CoreSkill, CoreSkillVersion)
                .join(CoreSkillVersion, CoreSkillVersion.id == CoreSkill.current_version_id)
                .where(or_(CoreSkill.workspace_id == identity.workspace_id,
                    CoreSkill.workspace_id.is_(None)),
                    or_(CoreSkill.user_id == identity.principal, CoreSkill.user_id.is_(None)))
                .order_by(CoreSkill.workspace_id.is_(None).desc(), CoreSkill.name))).all()
            skills = [{"id": str(skill.id), "name": skill.name, "source": version.source,
                       "version_number": version.version, "content_hash": version.content_hash,
                       "scenes": (version.scenario or "").splitlines(),
                       "allowlist": version.tool_allowlist or [],
                       "mine": skill.user_id == identity.principal}
                      for skill, version in rows]
            return {"skills": skills}

    @router.get("/api/core/skills/{skill_id}")
    async def detail(skill_id: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        factory = request.app.state.factory
        async with factory() as tx:
            version, skill = await _visible_version(tx, skill_id, identity)
            if version is None:
                raise HTTPException(404, "skill_not_found")
            spec = await spec_of(tx, version)
            return {"id": str(skill.id), "name": skill.name, "source": version.source,
                    "version_number": version.version, "content_hash": version.content_hash,
                    "spec": spec.model_dump(mode="json")}

    @router.post("/api/core/skills/draft")
    async def draft(body: dict, request: Request, identity: Identity = Depends(authenticated)):
        run_id = body.get("run_id")
        if not isinstance(run_id, str):
            raise HTTPException(400, "run_id_required")
        from backend.core.orm import CoreRun
        factory = request.app.state.factory
        try:
            async with factory() as tx:
                run = await tx.get(CoreRun, uuid.UUID(run_id))
                if run is None or run.workspace_id != identity.workspace_id or run.user_id != identity.principal:
                    raise HTTPException(404, "run_not_found") from None
                spec = await draft_from_run(tx, run.id)
                return {"draft": spec.model_dump(mode="json")}
        except EntityNotFound as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/core/skills", status_code=201)
    async def save(body: SaveSkillBody, request: Request, identity: Identity = Depends(authenticated)):
        factory = request.app.state.factory
        raw = dict(body.spec)
        if body.name:
            raw["name"] = body.name
        raw["source"] = "user"
        try:
            spec = SkillSpec.model_validate(raw)
        except ValueError as exc:
            raise HTTPException(400, f"invalid_skill: {exc}") from exc
        # User playbooks may only reference tools that exist in the newest catalog.
        from backend.core.tools.orm import ToolCatalogVersion
        from backend.core.tools.store import load_catalog
        async with factory.begin() as tx:
            version = await tx.scalar(select(ToolCatalogVersion.version)
                .order_by(ToolCatalogVersion.created_at.desc(), ToolCatalogVersion.version.desc()).limit(1))
            if version is None:
                raise HTTPException(409, "tool_catalog_missing") from None
            catalog = await load_catalog(tx, version)
            known = {tool.name for tool in catalog.snapshot.tools}
            unknown = sorted(set(spec.allowlist) - known)
            if unknown:
                raise HTTPException(400, f"unknown_tools: {unknown}") from None
            skill, saved = await upsert_skill(tx, spec,
                workspace_id=identity.workspace_id, user_id=identity.principal)
            return {"id": str(skill.id), "version_number": saved.version,
                    "content_hash": saved.content_hash}

    return router
