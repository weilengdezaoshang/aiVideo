"""Database documents with revision CAS and transactionally maintained asset references."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select, text

from backend.infrastructure.orm import Asset, AssetReference, Document, DocumentRequest
from backend.storyboard import validate_storyboard
from backend.timeline import validate_timeline

ASSET_URL = re.compile(r"/api/assets/([a-f0-9-]{36})(?:/|$)")


def project_document(row: Document) -> dict:
    return {**row.data, "id": str(row.id), "name": row.name, "revision": row.revision,
            "updatedAt": row.updated_at.isoformat()}


def asset_references(value) -> set[uuid.UUID]:
    found = set()
    def visit(node, key=""):
        if isinstance(node, dict):
            for name, item in node.items():
                visit(item, name)
        elif isinstance(node, list):
            for item in node:
                visit(item, key)
        elif isinstance(node, str):
            if node and key in {"assetId", "originalAssetId", "maskAssetId", "posterAssetId", "referenceAssetIds"}:
                try:
                    found.add(uuid.UUID(node))
                except ValueError:
                    raise HTTPException(400, "资产标识无效") from None
            for ident in ASSET_URL.findall(node):
                found.add(uuid.UUID(ident))
    visit(value)
    return found


class DatabaseDocuments:
    def __init__(self, factory):
        self.factory = factory

    async def get(self, workspace, ident):
        async with self.factory() as session:
            row = await session.get(Document, ident)
            if row is None or row.workspace_id != workspace:
                raise HTTPException(404, "文档不存在")
            return project_document(row)

    async def list(self, workspace):
        async with self.factory() as session:
            rows = (await session.scalars(select(Document).where(Document.workspace_id == workspace)
                .order_by(Document.updated_at.desc()).limit(500))).all()
            return [{"id": str(row.id), "name": row.name, "updatedAt": row.updated_at.isoformat(),
                     "objectCount": len(row.data.get("order", []))} for row in rows]

    async def create(self, workspace, name, request_id=None):
        name = str(name or "未命名画布").strip()[:100] or "未命名画布"
        fingerprint = hashlib.sha256(name.encode()).hexdigest()
        if request_id and len(request_id) > 128:
            raise HTTPException(400, "Idempotency-Key 过长")
        async with self.factory.begin() as session:
            if request_id:
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                    {"scope": f"document:{workspace}:{request_id}"})
                previous = await session.get(DocumentRequest, (workspace, request_id))
                if previous:
                    if previous.request_hash != fingerprint:
                        raise HTTPException(409, "同一请求标识不能更改参数")
                    row = await session.get(Document, previous.document_id)
                    return project_document(row)
            row = Document(workspace_id=workspace, name=name, revision=1,
                data={"objects": {}, "order": []}, updated_at=datetime.now(timezone.utc))
            session.add(row)
            await session.flush()
            if request_id:
                session.add(DocumentRequest(workspace_id=workspace, request_id=request_id,
                    request_hash=fingerprint, document_id=row.id))
            return project_document(row)

    async def save(self, workspace, ident, raw):
        if not isinstance(raw.get("objects"), dict) or len(raw["objects"]) > 5000:
            raise HTTPException(400, "objects 必须是对象且不超过 5000 项")
        if not isinstance(raw.get("order"), list) or any(not isinstance(i, str) for i in raw["order"]):
            raise HTTPException(400, "order 必须是对象 id 数组")
        if type(raw.get("baseRevision")) is not int or raw["baseRevision"] < 1:
            raise HTTPException(428, "必须提供 baseRevision")
        data = {k: raw[k] for k in (
            "objects", "order", "groups", "chat", "generations", "schemaVersion"
        ) if k in raw}
        for key, expected, maximum in (
            ("groups", dict, 200),
            ("chat", list, 500),
            ("generations", dict, 5000),
        ):
            if key in data and (not isinstance(data[key], expected) or len(data[key]) > maximum):
                raise HTTPException(400, f"{key} 格式错误或超限")
        for key, validate in (("timeline", validate_timeline), ("storyboard", validate_storyboard)):
            if key in raw:
                data[key] = validate(raw[key]) if raw[key] is not None else None
        if len(json.dumps(data).encode()) > 5 * 1024**2:
            raise HTTPException(413, "文档内容超过大小限制")
        async with self.factory.begin() as session:
            row = await session.get(Document, ident, with_for_update=True)
            if row is None or row.workspace_id != workspace:
                raise HTTPException(404, "文档不存在")
            if row.revision != raw["baseRevision"]:
                return None, project_document(row)
            for key in ("timeline", "storyboard", "generations"):
                if key not in raw and key in row.data:
                    data[key] = row.data[key]
            references = asset_references(data)
            if references:
                assets = list((await session.scalars(select(Asset).where(Asset.id.in_(references))
                    .order_by(Asset.id).with_for_update())).all())
                if len(assets) != len(references) or any(a.workspace_id != workspace for a in assets):
                    raise HTTPException(404, "引用资产不存在")
            await session.execute(delete(AssetReference).where(
                AssetReference.owner_type == "document", AssetReference.owner_id == str(ident)))
            for asset_id in references:
                session.add(AssetReference(asset_id=asset_id, owner_type="document", owner_id=str(ident)))
            row.data = data
            row.name = str(raw.get("name") or row.name).strip()[:100] or "未命名画布"
            row.revision += 1
            row.updated_at = datetime.now(timezone.utc)
            return project_document(row), None
