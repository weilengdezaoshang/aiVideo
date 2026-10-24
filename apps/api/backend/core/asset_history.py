"""Authorized immutable asset history projections; switching is owned by the worker."""

from uuid import UUID

from sqlalchemy import select

from backend.core.orm import CoreAsset, CoreAssetVersion
from backend.infrastructure.orm import Asset


async def asset_head(tx, *, asset_id: UUID, workspace_id: UUID, user_id: str) -> CoreAsset:
    asset = await tx.get(CoreAsset, asset_id)
    if (asset is None or asset.workspace_id != workspace_id or
            (asset.ownership != "team" and asset.user_id != user_id)):
        raise LookupError("asset_history_not_found")
    return asset


async def version_projection(tx, row: CoreAssetVersion) -> dict:
    stored = await tx.get(Asset, row.stored_asset_id) if row.stored_asset_id else None
    readable = bool(row.status == "normal" and stored and stored.workspace_id == row.workspace_id
                    and not (stored.details or {}).get("hostBlocked"))
    return {"id": str(row.id), "version": row.version, "kind": row.kind,
            "created_at": row.created_at.isoformat(), "status": row.status,
            "src": f"/api/assets/{stored.id}/content" if readable else None,
            "run_id": str(row.run_id) if row.run_id else None,
            "task_id": str(row.task_id) if row.task_id else None,
            "previous_version_id": str(row.previous_version_id) if row.previous_version_id else None}


async def versions(tx, *, asset_id: UUID, workspace_id: UUID, user_id: str,
                   before: int | None = None, limit: int) -> list[dict]:
    await asset_head(tx, asset_id=asset_id, workspace_id=workspace_id, user_id=user_id)
    query = select(CoreAssetVersion).where(CoreAssetVersion.asset_id == asset_id,
                                         CoreAssetVersion.workspace_id == workspace_id)
    if before is not None:
        query = query.where(CoreAssetVersion.version < before)
    rows = list(await tx.scalars(query.order_by(CoreAssetVersion.version.desc()).limit(limit)))
    return [await version_projection(tx, row) for row in rows]


async def compare(tx, *, asset_id: UUID, left: UUID, right: UUID, workspace_id: UUID,
                  user_id: str) -> dict:
    await asset_head(tx, asset_id=asset_id, workspace_id=workspace_id, user_id=user_id)
    result = []
    for ident in (left, right):
        row = await tx.get(CoreAssetVersion, ident)
        if row is None or row.asset_id != asset_id or row.workspace_id != workspace_id:
            raise LookupError("asset_version_not_found")
        result.append(await version_projection(tx, row))
    return {"asset_id": str(asset_id), "left": result[0], "right": result[1]}
