"""Offline JSON import into a new, memberless workspace. Never starts generation."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import uuid

from sqlalchemy import func, select, text

from backend.infrastructure.artifacts import ArtifactSpool
from backend.infrastructure.database import create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Asset, AssetReference, AuditEvent, Document, GenerationRequest, Job, MediaAlias, Workspace, WorkspaceMember
from backend.infrastructure.resilience import scope_key
from backend.production import load_routes
from backend.services.documents import asset_references


def read(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def date(value):
    if not value:
        return datetime.now(timezone.utc)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Legacy timestamp must include timezone")
    return result


def media_source(legacy, record):
    historical = "file" in record
    ext = Path(record["file"]).suffix.lstrip(".") if historical else record["ext"]
    source = legacy / "images" / record["file"] if historical else legacy / "assets" / str(uuid.UUID(record["id"])) / f"original.{ext}"
    if not source.resolve().is_relative_to(legacy) or not source.is_file():
        raise ValueError("Missing or unsafe legacy media; import aborted")
    return source, ext


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def import_legacy(factory, legacy: Path, media: Path, workspace: uuid.UUID,
                        routes: dict, actor: str) -> dict:
    legacy, media = legacy.resolve(), media.resolve()
    if not legacy.is_dir():
        raise ValueError("Legacy directory does not exist")
    lock = (legacy / ".server.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        jobs = read(legacy / "jobs.json", {}).get("jobs", [])
        history = read(legacy / "history.json", {}).get("records", [])
        documents = [read(path, {}) for path in sorted((legacy / "documents").glob("*.json"))
                     if not path.name.startswith("_")]
        asset_records = [read(path, {}) for path in sorted((legacy / "assets").glob("*/meta.json"))]
        hashes = {record["id"]: await asyncio.to_thread(file_hash, media_source(legacy, record)[0])
                  for record in asset_records + history}
        manifest = hashlib.sha256(json.dumps([jobs, history, documents, asset_records, hashes], sort_keys=True).encode()).hexdigest()
        report = {"manifest": manifest, "jobs": len(jobs), "documents": len(documents), "assets": 0, "unknown": 0}
        spool = ArtifactSpool(media)
        async with factory.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                                  {"scope": "import:" + str(workspace)})
            prior = (await session.scalars(select(AuditEvent).where(AuditEvent.workspace_id == workspace,
                AuditEvent.action == "import_legacy"))).one_or_none()
            if prior:
                if prior.details.get("manifest") != manifest:
                    raise ValueError("This workspace already has a different import")
                for asset in await session.scalars(select(Asset).where(Asset.workspace_id == workspace)):
                    await asyncio.to_thread(spool.verify, {"storage_key": asset.storage_key,
                        "bytes": asset.bytes, "sha256": asset.sha256})
                return {**prior.details, "replayed": True}
            for model in (Job, Asset, Document, WorkspaceMember):
                if await session.scalar(select(func.count()).select_from(model).where(model.workspace_id == workspace)):
                    raise ValueError("Import requires a new workspace without members or business data")
            if await session.get(Workspace, workspace, with_for_update=True) is None:
                session.add(Workspace(id=workspace, name="导入工作区"))
                await session.flush()
            assets, aliases = {}, {}
            for record in asset_records + history:
                ident = uuid.UUID(record["id"])
                if await session.get(Asset, ident) is not None:
                    raise ValueError("Legacy asset identifier conflicts with an existing asset")
                historical = "file" in record
                source, ext = media_source(legacy, record)
                artifact = await asyncio.to_thread(spool.save_file, uuid.uuid5(workspace, str(ident)), 0, source, ext)
                if artifact["sha256"] != hashes[record["id"]]:
                    raise ValueError("Source changed during offline import")
                kind = record.get("params", {}).get("kind", "image") if historical else record.get("kind", "image")
                details = {k: v for k, v in record.items() if k not in {"id", "ext", "kind", "width", "height", "bytes", "url", "file"}}
                details["history"] = historical
                row = Asset(id=ident, workspace_id=workspace, kind=kind, ext=ext,
                    width=record.get("width", record.get("params", {}).get("width", 0)),
                    height=record.get("height", record.get("params", {}).get("height", 0)),
                    bytes=artifact["bytes"], sha256=artifact["sha256"], storage_key=artifact["storage_key"],
                    details=details, created_at=date(record.get("createdAt")))
                if ident in assets:
                    raise ValueError("Duplicate legacy asset identifier")
                assets[ident] = (row, artifact)
                session.add(row)
                path = f"/images/{record['file']}" if historical else f"/assets/{ident}/original.{ext}"
                aliases[path] = ident
            await session.flush()
            report["assets"] = len(assets)
            for path, ident in aliases.items():
                session.add(MediaAlias(workspace_id=workspace, path=path, asset_id=ident))
            def rewrite(value):
                if isinstance(value, dict):
                    return {key: rewrite(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [rewrite(item) for item in value]
                if isinstance(value, str) and value in aliases:
                    return f"/api/assets/{aliases[value]}/content"
                return value
            document_ids = set()
            for original in documents:
                document = rewrite(original)
                ident = uuid.UUID(document["id"])
                document_ids.add(ident)
                session.add(Document(id=ident, workspace_id=workspace, name=document.get("name", "未命名画布"),
                    revision=document.get("revision", 1), updated_at=date(document.get("updatedAt")),
                    data={k: v for k, v in document.items() if k not in {"id", "name", "revision", "updatedAt"}}))
                for asset_id in asset_references(document):
                    if asset_id not in assets:
                        raise ValueError("Document has a missing asset reference")
                    session.add(AssetReference(asset_id=asset_id, owner_type="document", owner_id=str(ident)))
            await session.flush()
            for original in jobs:
                ident, params = uuid.UUID(original["id"]), dict(original["params"])
                kind = params.get("kind", "image")
                snapshot = dict(routes[kind])
                # The operator must supply the original endpoint/credential version;
                # the legacy actual model is fixed separately for each old task.
                snapshot["videoModel" if kind == "video" else "cloudModel"] = params.get("model", snapshot.get("cloudModel", ""))
                public_status = original.get("status")
                status = public_status if public_status in {"completed", "failed"} else "unknown"
                params.update({key: original[key] for key in ("clientRef", "requestId", "documentId") if original.get(key)})
                doc_id = uuid.UUID(original["documentId"]) if original.get("documentId") else None
                if doc_id and doc_id not in document_ids:
                    raise ValueError("Job has a missing document")
                row = Job(id=ident, workspace_id=workspace, document_id=doc_id, kind=kind, params=params,
                    status=status, phase="completed" if status == "completed" else "failed" if status == "failed" else "reconcile_exhausted",
                    provider_snapshot=snapshot, phase_payload={}, created_at=date(original.get("createdAt")),
                    state_version=1, progress=1 if status == "completed" else 0,
                    external_task_id=original.get("externalTaskId"))
                if status == "unknown":
                    report["unknown"] += 1
                    row.error_code, row.recovery = "UPSTREAM_UNKNOWN", "reconcile"
                    row.slot_scope = scope_key(snapshot.get("comfyUrl") if snapshot.get("provider") == "comfyui" else snapshot.get("cloudBaseUrl", ""),
                        snapshot.get("credentialRef", ""), params.get("model", ""), "model")
                    row.slot_acquired_at = datetime.now(timezone.utc)
                    # Import itself does not schedule network work. Explicit audit is
                    # required to validate legacy routing before any reconciliation.
                    row.reconcile_deadline = datetime.now(timezone.utc) + timedelta(days=1)
                else:
                    row.finished_at = date(original.get("finishedAt", original.get("createdAt")))
                results = original.get("images", [])
                if status == "completed" and not results:
                    raise ValueError("Completed legacy job is missing its outputs")
                if len(results) == 1:
                    asset_id = uuid.UUID(results[0]["id"])
                    if asset_id not in assets:
                        raise ValueError("Completed job output is missing")
                    row.phase_payload = {**assets[asset_id][1], "assetId": str(asset_id)}
                elif len(results) > 1:
                    row.kind = "batch"
                session.add(row)
                await session.flush()
                for index, result in enumerate(results):
                    asset_id = uuid.UUID(result["id"])
                    if asset_id not in assets:
                        raise ValueError("Job output is missing")
                    session.add(AssetReference(asset_id=asset_id, owner_type="job", owner_id=str(ident)))
                    if len(results) > 1:
                        child_id = uuid.uuid5(ident, f"imported-result:{index}")
                        session.add(Job(id=child_id, parent_id=ident, workspace_id=workspace, document_id=doc_id,
                            kind=kind, params={**params, "batchCount": 1}, status="completed", phase="completed",
                            provider_snapshot=snapshot, phase_payload={**assets[asset_id][1], "assetId": str(asset_id)},
                            state_version=1, progress=1, finished_at=row.finished_at or datetime.now(timezone.utc)))
                if original.get("requestId") and original.get("requestHash"):
                    session.add(GenerationRequest(workspace_id=workspace, request_id=original["requestId"],
                        request_hash=original["requestHash"], params=params, job_id=ident))
            session.add(AuditEvent(workspace_id=workspace, actor=actor, action="import_legacy",
                target=str(workspace), details=report))
        return report
    finally:
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--workspace", type=uuid.UUID, required=True)
    parser.add_argument("--original-provider-routes", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--quiesced", action="store_true", required=True)
    args = parser.parse_args()
    if not os.environ.get("AIVERO_DB_URL"):
        parser.error("Explicit AIVERO_DB_URL is required")
    async def run():
        engine = create_async_database_engine()
        try:
            return await import_legacy(create_async_session_factory(engine), args.legacy_root, args.media_root,
                args.workspace, load_routes(args.original_provider_routes), args.actor)
        finally:
            await engine.dispose()
    print(json.dumps(asyncio.run(run())))


if __name__ == "__main__":
    main()
