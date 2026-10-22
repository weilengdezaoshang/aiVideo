"""Authenticated backend-only supervisor endpoints; masks never leave the projection."""
import uuid
from typing import Literal
from fastapi import Depends, HTTPException
from pydantic import Field
from backend.core.contracts import Contract, CoreSettings
from backend.services.identity import Identity, authenticated
from backend.core.orm import CoreRun
from .service import start, progress, answer


class Start(Contract):
    document_id: uuid.UUID
    request_key: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1)
    selected_assets: list[uuid.UUID] = Field(default_factory=list)
    action: Literal["cutout", "tool"] | None = None
    tool: str | None = None
    parameters: dict = Field(default_factory=dict)
    placement: dict = Field(default_factory=dict)
    anchor_id: str | None = None
    invariants: dict = Field(default_factory=dict)
    mode: Literal["auto", "plan"] = "auto"
    credits_budget: int = Field(default=0, ge=0)
    token_budget: int = Field(default_factory=lambda: CoreSettings().tokens_limit, ge=0)
    max_thought_rounds: int = Field(default_factory=lambda: CoreSettings().max_thought_rounds, gt=0)


class Answer(Contract):
    option_id: str = Field(min_length=1)
    request_key: str = Field(min_length=1, max_length=128)


def install(app):
    @app.post("/api/core/runs", status_code=201)
    async def create(body: Start, identity: Identity = Depends(authenticated)):
        request = body.model_dump(mode="json", exclude={"document_id", "request_key", "credits_budget", "token_budget", "max_thought_rounds", "mode"})
        try:
            async with app.state.factory.begin() as tx:
                run = await start(tx, workspace_id=identity.workspace_id, user_id=identity.principal,
                    document_id=body.document_id, request_key=body.request_key, request=request,
                    budget=body.credits_budget, token_budget=body.token_budget, max_rounds=body.max_thought_rounds, mode=body.mode)
                return await progress(tx, run)
        except LookupError:
            raise HTTPException(404, "画布不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/core/runs/{run_id}")
    async def get(run_id: uuid.UUID, identity: Identity = Depends(authenticated)):
        async with app.state.factory() as tx:
            run = await tx.get(CoreRun, run_id)
            if run is None or run.workspace_id != identity.workspace_id or run.user_id != identity.principal:
                raise HTTPException(404, "Run 不存在")
            return await progress(tx, run)

    @app.post("/api/core/tasks/{task_id}/answer")
    async def reply(task_id: uuid.UUID, body: Answer, identity: Identity = Depends(authenticated)):
        try:
            async with app.state.factory.begin() as tx:
                task = await answer(tx, task_id=task_id, option_id=body.option_id, request_key=body.request_key,
                    workspace_id=identity.workspace_id, user_id=identity.principal)
                return {"task_id": str(task.id), "status": task.status}
        except LookupError:
            raise HTTPException(404, "任务不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None


async def workspace_progress(factory, identity):
    from sqlalchemy import select
    from .contracts import Settings
    async with factory() as tx:
        runs = await tx.scalars(select(CoreRun).where(CoreRun.workspace_id == identity.workspace_id,
            CoreRun.user_id == identity.principal).order_by(CoreRun.created_at.desc()).limit(Settings().scan_limit))
        return [await progress(tx, run) for run in runs]
