"""Read-only internal diagnostics: workspace owner/admin, never public or cross-tenant."""

import uuid
from fastapi import Depends, HTTPException, Query
from sqlalchemy import text
from backend.services.identity import Identity, authenticated
from backend.core.orm import CoreRun
from .reader import report, page, SECTIONS


def install(app):
    async def scoped(tx, ident, identity):
        if identity.role not in {"owner", "admin"}:
            raise HTTPException(403, "需要工作区管理员权限")
        run = await tx.get(CoreRun, ident)
        if run is None or run.workspace_id != identity.workspace_id:
            raise HTTPException(404, "Run 不存在")
        return run

    @app.get("/api/internal/core/runs/{run_id}", include_in_schema=False)
    async def detail(run_id: uuid.UUID, identity: Identity = Depends(authenticated)):
        async with app.state.factory() as tx:
            await tx.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            return await report(tx, await scoped(tx, run_id, identity))

    @app.get("/api/internal/core/runs/{run_id}/{section}", include_in_schema=False)
    async def more(
        run_id: uuid.UUID,
        section: str,
        after: str | None = Query(default=None),
        identity: Identity = Depends(authenticated),
    ):
        if section not in SECTIONS:
            raise HTTPException(404, "未知记录类型")
        try:
            async with app.state.factory() as tx:
                await tx.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                return await page(tx, await scoped(tx, run_id, identity), section, after)
        except ValueError:
            raise HTTPException(400, "分页游标无效") from None
