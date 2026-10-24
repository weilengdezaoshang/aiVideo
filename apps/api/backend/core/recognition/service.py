"""Recognition intake: idempotent enqueue after upload or generation ingest."""
from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.infrastructure.orm import Asset

from .contracts import RecognitionSettings
from .orm import CoreRecognitionJob


def job_request_key(stored_asset: Asset) -> str:
    return hashlib.sha256(f"recognition:{stored_asset.id}:{stored_asset.sha256}".encode()).hexdigest()


async def enqueue_recognition(tx, *, workspace_id: uuid.UUID, stored_asset: Asset,
                              requested_by: str, source: str,
                              settings: RecognitionSettings | None = None) -> CoreRecognitionJob | None:
    """Only images enter the pipeline; every failure mode stays silent for the caller."""
    settings = settings or RecognitionSettings()
    if not settings.enabled or not isinstance(stored_asset, Asset) or stored_asset.kind != "image":
        return None
    if stored_asset.workspace_id != workspace_id:
        return None
    key = job_request_key(stored_asset)
    existing = await tx.scalar(select(CoreRecognitionJob).where(CoreRecognitionJob.request_key == key))
    if existing is not None:
        return existing
    row = CoreRecognitionJob(workspace_id=workspace_id, stored_asset_id=stored_asset.id,
        requested_by=requested_by[:200] or "unknown", source=source, request_key=key)
    try:
        async with tx.begin_nested():
            tx.add(row)
            await tx.flush()
        return row
    except IntegrityError:
        return await tx.scalar(select(CoreRecognitionJob).where(CoreRecognitionJob.request_key == key))
