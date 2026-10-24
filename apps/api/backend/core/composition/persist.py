"""Store one text layout on the media host and a text asset version. Replay reuses the attempt path."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from sqlalchemy import select

from backend.core.contracts import AssetContent
from backend.core.orm import CoreAssetVersion
from backend.core.store import create_asset
from backend.infrastructure.artifacts import ArtifactSpool
from backend.infrastructure.orm import Asset, AssetReference

_TEXT_FIELDS = ("copy", "font_id", "font_version", "font_sha256", "size", "color", "alignment",
                "region", "preset", "canvas", "box", "lines")


def materialize_text_reference(supplied: dict, *, storage_key: str, sha256: str, asset_id: str,
                               blob: bytes, core_asset_id: str | None, core_asset_version_id: str | None) -> dict:
    """Database file wins. Caller-supplied copy, font and line boxes are ignored."""
    if hashlib.sha256(blob).hexdigest() != sha256:
        raise ValueError("text_hash_mismatch")
    saved = json.loads(blob)
    if not isinstance(saved, dict) or "copy" not in saved:
        raise ValueError("text_hash_mismatch")
    trusted = {key: saved[key] for key in _TEXT_FIELDS if key in saved}
    trusted.update(asset_id=asset_id, media_type="text", storage_key=storage_key, sha256=sha256,
                   ext="json")
    if core_asset_id:
        trusted["core_asset_id"] = core_asset_id
    if core_asset_version_id:
        trusted["core_asset_version_id"] = core_asset_version_id
    return trusted


async def persist_text_layout(tx, root: Path, attempt_id, run, task, layout: dict, *, job_id) -> dict:
    """Write one text asset with run, task and user lineage. Replay keeps the same rows."""
    if not isinstance(layout, dict) or "copy" not in layout or task.run_id != run.id:
        raise ValueError("param_invalid")
    key = f"text-layouts/{attempt_id}.json"
    existing = await tx.scalar(select(Asset).where(Asset.workspace_id == run.workspace_id, Asset.storage_key == key))
    if existing is not None:
        version = await tx.scalar(select(CoreAssetVersion).where(
            CoreAssetVersion.stored_asset_id == existing.id).order_by(CoreAssetVersion.version.desc()).limit(1))
        payload = json.loads((root / key).read_text(encoding="utf-8"))
        if version is not None:
            payload["core_asset_id"] = str(version.asset_id)
            payload["core_asset_version_id"] = str(version.id)
        payload["asset_id"] = str(existing.id)
        payload["media_type"] = "text"
        return payload
    body = json.dumps(layout, ensure_ascii=False, sort_keys=True).encode()
    target = root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    ArtifactSpool._atomic(target, body)
    digest = hashlib.sha256(body).hexdigest()
    asset = Asset(id=uuid.uuid4(), workspace_id=run.workspace_id, kind="text", ext="json",
        width=layout["canvas"]["width"], height=layout["canvas"]["height"], bytes=len(body), sha256=digest,
        storage_key=key, details={"history": True, "coreRunId": str(run.id), "coreTaskId": str(task.id), "text": True})
    tx.add(asset)
    await tx.flush()
    tx.add(AssetReference(asset_id=asset.id, owner_type="job", owner_id=str(job_id)))
    content = AssetContent(kind="text", category="artifact", storage={"asset_id": str(asset.id)},
        stored_asset_id=asset.id, content_hash=digest, user_id=run.user_id, run_id=run.id, task_id=task.id,
        width=asset.width, height=asset.height, size_bytes=asset.bytes, layers={"role": "text", "copy": layout["copy"]},
        metadata_json={"font_id": layout["font_id"], "font_version": layout["font_version"],
                       "font_sha256": layout["font_sha256"]})
    core = await create_asset(tx, run.workspace_id, run.user_id, "personal", content)
    return {**layout, "asset_id": str(asset.id), "media_type": "text",
            "core_asset_id": str(core.id), "core_asset_version_id": str(core.current_version_id)}
