"""Append an image-edit version on an existing chain. Does not write the canvas.

Codex publish hook, called by Worker.publish while the Run and document locks are held:
  from backend.core.asset_edit import append_image_edit, PUBLISH_HOOK
  if tool capability is image_edit or inpaint:
      edited = await append_image_edit(tx, capability=capability, resolved_asset_id=image_id, ...)
      use edited.core_asset_id and edited.version_id on the task output
  cutout stays a new artifact and must not call this helper.
This module does not import the execution engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select

from backend.core.asset_history import asset_head
from backend.core.contracts import AssetContent
from backend.core.orm import CoreAsset, CoreAssetVersion
from backend.core.states import VersionConflict
from backend.core.store import append_asset_version, create_asset
from backend.infrastructure.orm import Asset, AssetReference

PUBLISH_HOOK = (
    "Worker.publish: for image_edit/inpaint call append_image_edit under the existing "
    "Run/document transaction, after the document lock, and store core_asset_id/version_id. "
    "Do not call it for cutout. The helper locks the source Asset row and does not retry. "
    "On asset_version_conflict the caller applies its own policy and never updates an old version."
)

EDIT_CAPABILITIES = frozenset({"image_edit", "inpaint"})


class EditRejected(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EditVersion:
    core_asset_id: UUID
    version_id: UUID
    version: int


def capability_of(tool_name: str) -> str | None:
    if tool_name == "cutout" or tool_name.startswith("cutout_"):
        raise EditRejected("cutout_preserves_original_as_new_artifact")
    for name in EDIT_CAPABILITIES:
        if tool_name == name or tool_name.startswith(name + "_"):
            return name
    return None


async def _source_version(tx, resolved_asset_id: UUID, workspace_id: UUID) -> CoreAssetVersion | None:
    return await tx.scalar(select(CoreAssetVersion).where(
        CoreAssetVersion.workspace_id == workspace_id,
        or_(CoreAssetVersion.id == resolved_asset_id, CoreAssetVersion.stored_asset_id == resolved_asset_id),
    ).order_by(CoreAssetVersion.version.desc()).limit(1))


async def _stored_asset(tx, asset_id: UUID, workspace_id: UUID) -> Asset:
    stored = await tx.scalar(select(Asset).where(Asset.id == asset_id).with_for_update())
    if stored is None or stored.workspace_id != workspace_id:
        raise EditRejected("original_not_authorized")
    if (stored.details or {}).get("hostBlocked"):
        raise EditRejected("source_host_blocked")
    if not stored.sha256 or len(stored.sha256) != 64:
        raise EditRejected("original_hash_missing")
    referenced = await tx.scalar(select(AssetReference.id).where(AssetReference.asset_id == stored.id).limit(1))
    if referenced is None:
        raise EditRejected("original_not_referenced")
    return stored


def _content_from_asset(stored: Asset, user_id: str) -> AssetContent:
    if stored.kind != "image":
        raise EditRejected("original_not_image")
    return AssetContent(
        kind="image", category="artifact", storage={"asset_id": str(stored.id)},
        stored_asset_id=stored.id, content_hash=stored.sha256, user_id=user_id,
        width=stored.width or None, height=stored.height or None, size_bytes=stored.bytes or None)


async def append_image_edit(tx, *, tool_name: str, resolved_asset_id: UUID, workspace_id: UUID,
                            user_id: str, run_id: UUID, task_id: UUID, result: AssetContent,
                            original: AssetContent | None = None, expected_version: int | None = None,
                            ownership: str = "personal") -> EditVersion:
    """Append one result. Lineage points at the resolved source version, not a newer head.

    `original` is ignored. The stored Asset row, its hash and an AssetReference are the source.
    Concurrent callers serialize on that Asset row. This function does not retry a conflict.
    """
    del original
    if capability_of(tool_name) is None:
        raise EditRejected("not_an_image_edit")
    if result.kind != "image" or result.run_id != run_id or result.task_id != task_id or result.user_id != user_id:
        raise EditRejected("edit_result_identity_mismatch")
    source = await _source_version(tx, resolved_asset_id, workspace_id)
    if source is None:
        stored = await _stored_asset(tx, resolved_asset_id, workspace_id)
        source = await _source_version(tx, resolved_asset_id, workspace_id)
        if source is None:
            asset = await create_asset(tx, workspace_id, user_id, ownership, _content_from_asset(stored, user_id))
            source = await tx.scalar(select(CoreAssetVersion).where(
                CoreAssetVersion.asset_id == asset.id, CoreAssetVersion.version == 1))
    try:
        asset = await asset_head(tx, asset_id=source.asset_id, workspace_id=workspace_id, user_id=user_id)
    except LookupError as exc:
        raise EditRejected("asset_not_authorized") from exc
    if source.status != "normal":
        raise EditRejected("source_version_not_normal")
    await _stored_asset(tx, source.stored_asset_id, workspace_id)
    if source.content_hash != (await tx.get(Asset, source.stored_asset_id)).sha256:
        raise EditRejected("original_hash_mismatch")
    head = await tx.scalar(select(CoreAssetVersion).where(
        CoreAssetVersion.asset_id == asset.id, CoreAssetVersion.version == asset.current_version_number))
    if head is None:
        raise EditRejected("asset_head_missing")
    expected = asset.version if expected_version is None else expected_version
    try:
        row = await append_asset_version(tx, asset.id, expected, result, parents=(source.id,), relation="edited")
    except VersionConflict as exc:
        raise EditRejected("asset_version_conflict") from exc
    fresh = await tx.get(CoreAsset, asset.id)
    if fresh.current_version_id != row.id or row.previous_version_id != head.id:
        raise EditRejected("asset_version_conflict")
    return EditVersion(core_asset_id=asset.id, version_id=row.id, version=row.version)
