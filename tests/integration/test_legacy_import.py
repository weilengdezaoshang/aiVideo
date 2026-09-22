import asyncio
import json
import os
import uuid

from PIL import Image
import pytest
from sqlalchemy import select

from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Asset, Document, Job, MediaAlias
from backend.tools.import_legacy import import_legacy

DB = os.environ.get("AIVERO_DURABLE_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated PostgreSQL URL required")


def test_offline_import_preserves_ids_references_and_unknown_occupancy(tmp_path):
    legacy, media = tmp_path / "legacy", tmp_path / "media"
    (legacy / "documents").mkdir(parents=True)
    (legacy / "images").mkdir()
    asset_id, doc_id, job_id, unknown_id = [str(uuid.uuid4()) for _ in range(4)]
    filename = "img_0123456789abcdef.png"
    Image.new("RGB", (64, 64), "blue").save(legacy / "images" / filename)
    record = {"id": asset_id, "jobId": job_id, "file": filename, "url": "/images/" + filename,
        "params": {"prompt": "test", "model": "old-model", "kind": "image"}, "createdAt": "2026-01-01T00:00:00Z"}
    (legacy / "history.json").write_text(json.dumps({"records": [record]}))
    jobs = [{"id": job_id, "status": "completed", "params": record["params"], "images": [record]},
            {"id": unknown_id, "status": "running", "params": record["params"], "externalTaskId": "upstream-1"}]
    (legacy / "jobs.json").write_text(json.dumps({"jobs": jobs}))
    document = {"id": doc_id, "name": "import", "revision": 7, "objects": {"a": {"src": record["url"]}}, "order": ["a"]}
    source = legacy / "documents" / (doc_id + ".json")
    source.write_text(json.dumps(document))
    original_bytes = source.read_bytes()

    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        workspace = uuid.uuid4()
        try:
            routes = {"image": {"provider": "mock", "cloudModel": "old-model", "configVersion": "legacy-v1"}}
            result = await import_legacy(factory, legacy, media, workspace, routes, "test-operator")
            assert result["assets"] == 1 and result["unknown"] == 1
            assert (await import_legacy(factory, legacy, media, workspace, routes, "test-operator"))["replayed"]
            async with factory() as session:
                document_row = await session.get(Document, uuid.UUID(doc_id))
                assert document_row.revision == 7
                assert document_row.data["objects"]["a"]["src"] == f"/api/assets/{asset_id}/content"
                unknown = await session.get(Job, uuid.UUID(unknown_id))
                assert unknown.status == "unknown" and unknown.slot_scope
                assert unknown.phase == "reconcile_exhausted" and unknown.external_task_id == "upstream-1"
                assert len(list(await session.scalars(select(MediaAlias).where(MediaAlias.workspace_id == workspace)))) == 1
                saved = await session.get(Asset, uuid.UUID(asset_id))
                assert (media / saved.storage_key).is_file()
            assert source.read_bytes() == original_bytes
            from backend.tools.media_audit import audit_media
            audit = await audit_media(factory, media)
            # Other workspaces share this test database but not this fixture's
            # media root; this import's own asset must still be verifiable.
            assert asset_id not in {item["assetId"] for item in audit["failures"]}
            Image.new("RGB", (64, 64), "red").save(legacy / "images" / filename)
            with pytest.raises(ValueError, match="different import"):
                await import_legacy(factory, legacy, media, workspace, routes, "test-operator")
        finally:
            await engine.dispose()
    asyncio.run(scenario())
