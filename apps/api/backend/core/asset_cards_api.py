"""Asset-card HTTP surface: user-owned card CRUD with CAS and delisting.

All writes go through the card domain service, so every change appends a version
and notifies active runs. Cards are the only asset layer the supervisor cites
by id; this router never touches canvas documents.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.core.asset_cards import (CARD_KINDS, CardRejected, create_card, load_card,
    set_card_status, update_card, version_view)
from .states import EntityNotFound
from backend.core.orm import CoreAsset, CoreAssetVersion
from backend.services.identity import Identity, authenticated
from sqlalchemy import select


class CreateCardBody(BaseModel):
    card_kind: str
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    source_asset_id: uuid.UUID | None = None
    starred: bool = False


class UpdateCardBody(BaseModel):
    expected_version_number: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    starred: bool | None = None


def _conflict(code: str):
    status = 404 if code in {"card_not_found"} else 409
    return HTTPException(status, code)


def build_cards_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/core/cards", status_code=201)
    async def create(body: CreateCardBody, request: Request, identity: Identity = Depends(authenticated)):
        if body.card_kind not in CARD_KINDS:
            raise HTTPException(400, "invalid_card_kind")
        factory = request.app.state.factory
        try:
            async with factory.begin() as tx:
                stored = None
                if body.source_asset_id is not None:
                    from backend.infrastructure.orm import Asset
                    stored = await tx.get(Asset, body.source_asset_id)
                    if stored is None or stored.workspace_id != identity.workspace_id:
                        raise HTTPException(400, "card_source_unavailable") from None
                card, version = await create_card(tx, workspace_id=identity.workspace_id,
                    user_id=identity.principal, card_kind=body.card_kind, name=body.name,
                    description=body.description, source_stored_asset=stored, starred=body.starred)
                return version_view(card, version)
        except CardRejected as exc:
            raise _conflict(exc.code) from exc

    @router.get("/api/core/cards")
    async def listing(request: Request, identity: Identity = Depends(authenticated),
                      kind: str | None = None, limit: int = 100):
        factory = request.app.state.factory
        async with factory() as tx:
            rows = (await tx.execute(select(CoreAsset, CoreAssetVersion)
                .join(CoreAssetVersion, CoreAssetVersion.id == CoreAsset.current_version_id)
                .where(CoreAsset.workspace_id == identity.workspace_id,
                    CoreAssetVersion.category == "card")
                .order_by(CoreAsset.updated_at.desc()).limit(max(1, min(limit, 500))))).all()
        views = [version_view(card, version) for card, version in rows]
        if kind:
            views = [view for view in views if view["card_kind"] == kind]
        return {"cards": views}

    @router.get("/api/core/cards/{card_id}")
    async def detail(card_id: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        factory = request.app.state.factory
        try:
            async with factory() as tx:
                card, version = await load_card(tx, card_id, identity.workspace_id)
                return version_view(card, version)
        except EntityNotFound:
            raise HTTPException(404, "card_not_found") from None

    @router.patch("/api/core/cards/{card_id}")
    async def update(card_id: uuid.UUID, body: UpdateCardBody, request: Request,
                     identity: Identity = Depends(authenticated)):
        factory = request.app.state.factory
        try:
            async with factory.begin() as tx:
                version = await update_card(tx, card_id, identity.workspace_id,
                    body.expected_version_number, name=body.name, description=body.description,
                    starred=body.starred)
                card = await tx.get(CoreAsset, card_id)
                return version_view(card, version)
        except CardRejected as exc:
            raise _conflict(exc.code) from exc
        except EntityNotFound:
            raise HTTPException(404, "card_not_found") from None

    @router.post("/api/core/cards/{card_id}/report")
    async def report(card_id: uuid.UUID, body: UpdateCardBody, request: Request,
                     identity: Identity = Depends(authenticated)):
        factory = request.app.state.factory
        try:
            async with factory.begin() as tx:
                version = await set_card_status(tx, card_id, identity.workspace_id,
                    body.expected_version_number, status="reported")
                card = await tx.get(CoreAsset, card_id)
                return version_view(card, version)
        except CardRejected as exc:
            raise _conflict(exc.code) from exc
        except EntityNotFound:
            raise HTTPException(404, "card_not_found") from None

    @router.post("/api/core/cards/{card_id}/remove")
    async def remove(card_id: uuid.UUID, body: UpdateCardBody, request: Request,
                     identity: Identity = Depends(authenticated)):
        factory = request.app.state.factory
        try:
            async with factory.begin() as tx:
                version = await set_card_status(tx, card_id, identity.workspace_id,
                    body.expected_version_number, status="removed")
                card = await tx.get(CoreAsset, card_id)
                return version_view(card, version)
        except CardRejected as exc:
            raise _conflict(exc.code) from exc
        except EntityNotFound:
            raise HTTPException(404, "card_not_found") from None

    return router
