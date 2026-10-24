"""Recognition daemon step: claim job, analyze outside the transaction, apply in one.

Crash safety: a lease token fences stale owners; the applied recognition is
stamped with the source content hash so a recovered job recognizes an already
written result instead of appending twice.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from backend.agent.media.catalog import HostError
from backend.agent.media.inspection import managed_path
from backend.agent.providers import ModelCallError
from backend.core.orm import CoreAsset, CoreAssetVersion
from backend.core.contracts import AssetContent
from backend.core.states import VersionConflict
from backend.core.store import append_asset_version, create_asset
from backend.infrastructure.orm import Asset

from .analyzer import Analyzer, build_analyzer
from .contracts import RecognitionSettings
from .orm import CoreRecognitionJob


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_artifact_wrapper(tx, stored: Asset, *, user_id: str):
    """Uploads have no core asset yet; find or create the artifact wrapper."""
    existing = await tx.scalar(select(CoreAsset).join(CoreAssetVersion,
        CoreAssetVersion.id == CoreAsset.current_version_id).where(
        CoreAssetVersion.stored_asset_id == stored.id, CoreAssetVersion.category == "artifact",
        CoreAsset.workspace_id == stored.workspace_id).order_by(CoreAsset.created_at.desc()).limit(1))
    if existing is not None:
        return existing
    content = AssetContent(kind=stored.kind, category="artifact", storage={"asset_id": str(stored.id)},
        stored_asset_id=stored.id, content_hash=stored.sha256, user_id=user_id,
        width=stored.width, height=stored.height, size_bytes=stored.bytes)
    return await create_asset(tx, stored.workspace_id, user_id, "personal", content)


async def apply_recognition(tx, job: CoreRecognitionJob, stored: Asset, recognition: dict) -> tuple[uuid.UUID, uuid.UUID]:
    """Write recognition as a new asset version; idempotent on the content hash."""
    wrapper = await ensure_artifact_wrapper(tx, stored, user_id=job.requested_by)
    head = await tx.get(CoreAssetVersion, wrapper.current_version_id)
    stamped = {**recognition, "content_hash": stored.sha256, "source": "vision"}
    if (head.recognition or {}).get("content_hash") == stamped["content_hash"] and head.recognition.get("source") == "vision":
        return wrapper.id, head.id
    content = AssetContent(kind=head.kind, category=head.category, card_kind=head.card_kind,
        starred=head.starred, recognition=stamped, layers=head.layers or {},
        metadata_json=head.metadata_json or {}, storage=head.storage or {},
        stored_asset_id=head.stored_asset_id, content_hash=head.content_hash,
        mime_type=head.mime_type, size_bytes=head.size_bytes, width=head.width,
        height=head.height, duration_ms=head.duration_ms, status=head.status,
        run_id=head.run_id, task_id=head.task_id, user_id=head.user_id)
    try:
        version = await append_asset_version(tx, wrapper.id, wrapper.version, content,
            parents=(head.id,), relation="recognition")
    except VersionConflict:
        wrapper = await tx.get(CoreAsset, wrapper.id, populate_existing=True)
        head = await tx.get(CoreAssetVersion, wrapper.current_version_id)
        if (head.recognition or {}).get("content_hash") == stamped["content_hash"] and head.recognition.get("source") == "vision":
            return wrapper.id, head.id
        version = await append_asset_version(tx, wrapper.id, wrapper.version, content,
            parents=(head.id,), relation="recognition")
    return wrapper.id, version.id


class RecognitionWorker:
    def __init__(self, factory, root: Path, analyzer: Analyzer | None = None, *,
                 settings: RecognitionSettings | None = None):
        self.factory, self.root = factory, Path(root)
        self.settings = settings or RecognitionSettings()
        self.analyzer = analyzer

    async def run_once(self) -> int:
        analyzer = self.analyzer or build_analyzer(self.settings)
        now = _now()
        claimed: list[tuple[uuid.UUID, uuid.UUID]] = []
        async with self.factory.begin() as tx:
            rows = list((await tx.scalars(select(CoreRecognitionJob).where(
                CoreRecognitionJob.status.in_(("pending", "claimed")))
                .limit(self.settings.scan_limit * 3).with_for_update(skip_locked=True))))
            for job in rows:
                if job.status == "claimed" and job.lease_until and job.lease_until > now:
                    continue
                if job.status == "pending" and job.next_at > now:
                    continue
                if job.attempt >= self.settings.max_attempts and job.status == "pending":
                    job.status = "failed"
                    job.error = {"code": "attempts_exhausted"}
                    continue
                job.status, job.lease_token = "claimed", uuid.uuid4()
                job.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
                claimed.append((job.id, job.lease_token))
            if not analyzer.available:
                for job in rows:
                    if job.status == "claimed":
                        job.status, job.lease_token, job.lease_until = "pending", None, None
                        job.next_at = now + timedelta(seconds=self.settings.retry_backoff_seconds)
                return 0
        applied = 0
        for job_id, token in claimed:
            if await self._process(job_id, token, analyzer):
                applied += 1
        return applied

    async def _resolve_claim(self, tx, job_id, token):
        job = await tx.scalar(select(CoreRecognitionJob).where(
            CoreRecognitionJob.id == job_id).with_for_update(skip_locked=True))
        if job is None or job.status != "claimed" or job.lease_token != token:
            return job, None
        stored = await tx.get(Asset, job.stored_asset_id)
        return job, stored

    async def _process(self, job_id: uuid.UUID, token: uuid.UUID, analyzer: Analyzer) -> bool:
        async with self.factory.begin() as tx:
            job, stored = await self._resolve_claim(tx, job_id, token)
            if stored is None:
                await self._finish(tx, job, token, "skipped", error={"code": "asset_missing"})
                return False
            try:
                path = managed_path(self.root, stored)
            except HostError:
                await self._finish(tx, job, token, "skipped", error={"code": "image_unreadable"})
                return False
            try:
                data = await asyncio.to_thread(path.read_bytes)
            except OSError:
                await self._finish(tx, job, token, "skipped", error={"code": "image_unreadable"})
                return False
        if len(data) > self.settings.max_image_bytes:
            async with self.factory.begin() as tx:
                job, _ = await self._resolve_claim(tx, job_id, token)
                await self._finish(tx, job, token, "skipped", error={"code": "image_too_large"})
            return False
        try:
            result = await asyncio.wait_for(analyzer.recognize(data, ext=stored.ext or "png"),
                timeout=self.settings.timeout_seconds)
            recognition = result.model_dump(mode="json")
            failure = None
        except (ModelCallError, TimeoutError, OSError, ValueError) as exc:
            recognition, failure = None, {"code": getattr(exc, "code", None) or "analyze_failed",
                "unknown": bool(getattr(exc, "unknown", False))}
        async with self.factory.begin() as tx:
            job, stored = await self._resolve_claim(tx, job_id, token)
            if stored is None:
                await self._finish(tx, job, token, "skipped", error={"code": "asset_missing"})
                return False
            if failure is not None:
                await self._fail(tx, job, token, failure)
                return False
            try:
                core_asset_id, version_id = await apply_recognition(tx, job, stored, recognition)
            except VersionConflict:
                await self._fail(tx, job, token, {"code": "version_conflict"})
                return False
            await self._finish(tx, job, token, "succeeded",
                result={"core_asset_id": str(core_asset_id), "core_asset_version_id": str(version_id),
                        "recognition": recognition},
                core_asset_id=core_asset_id, core_asset_version_id=version_id)
            return True

    async def _finish(self, tx, job, token, status: str, *, result: dict | None = None,
                      error: dict | None = None, core_asset_id=None, core_asset_version_id=None):
        if job is None or job.status != "claimed" or job.lease_token != token:
            return
        job.status, job.result, job.error = status, result or {}, error
        job.lease_token, job.lease_until = None, None
        if core_asset_id is not None:
            job.core_asset_id = core_asset_id
        if core_asset_version_id is not None:
            job.core_asset_version_id = core_asset_version_id

    async def _fail(self, tx, job, token, failure: dict):
        job.attempt += 1
        if job.attempt >= self.settings.max_attempts:
            await self._finish(tx, job, token, "failed", error=failure)
        else:
            job.status, job.lease_token, job.lease_until = "pending", None, None
            job.next_at = _now() + timedelta(seconds=self.settings.retry_backoff_seconds)
            job.error = failure


async def install(factory, settings: RecognitionSettings | None = None) -> RecognitionWorker:
    """Fail closed when migration 0026 is absent. Does not start a daemon."""
    async with factory.begin() as tx:
        await tx.execute(select(CoreRecognitionJob.id).limit(0))
    return RecognitionWorker(factory, Path("."), settings=settings)
