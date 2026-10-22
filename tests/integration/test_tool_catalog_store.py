"""Opt-in isolated PostgreSQL tests: immutable publication and locked old versions."""
import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.agent.media.catalog import Catalog
from backend.core.tools import CatalogSnapshot, ToolCatalog, ToolSettings, build_catalog
from backend.core.tools.store import CatalogVersionConflict, get_tool, load_catalog, publish
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory

DB = os.environ.get("AIVERO_TOOL_TEST_DB")
pytestmark = pytest.mark.skipif(not DB, reason="explicit isolated migrated tool database required")


def test_publish_new_version_preserves_locked_old_definition():
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        old_version, new_version = uuid.uuid4().hex, uuid.uuid4().hex
        old = build_catalog(Catalog.load(None), old_version)
        new = build_catalog(Catalog.load(None), new_version, ToolSettings(cutout_credits=3))
        try:
            async with factory.begin() as tx:
                lock = await publish(tx, old, published_by="integration")
                from backend.core.contracts import RunInput
                from backend.core.orm import CoreSession
                from backend.core.store import create_run
                from backend.infrastructure.orm import Workspace
                workspace_id = uuid.uuid4()
                tx.add(Workspace(id=workspace_id))
                await tx.flush()
                session = CoreSession(workspace_id=workspace_id, source_canvas_id=uuid.uuid4(),
                                      user_id="test", language="zh-CN")
                tx.add(session)
                await tx.flush()
                run = await create_run(tx, RunInput(session_id=session.id, workspace_id=workspace_id,
                    user_id="test", request_key=uuid.uuid4().hex, request_hash="a" * 64,
                    snapshot={}, scope={}, invariants={}, version_lock={"tool_catalog": lock.model_dump()}))
                run_id = run.id
            async with factory.begin() as tx:
                await publish(tx, new, published_by="integration")
            async with factory.begin() as tx:
                from backend.core.orm import CoreRun
                from backend.core.tools import VersionLock
                saved_run = await tx.get(CoreRun, run_id)
                saved_lock = VersionLock.model_validate(saved_run.version_lock["tool_catalog"])
                assert saved_lock == lock
                loaded = await load_catalog(tx, saved_lock.version, expected_lock=saved_lock)
                assert loaded.lock == lock
                assert (await get_tool(tx, "cutout", old_version)).price.credits == 0
                assert (await get_tool(tx, "cutout", new_version)).price.credits == 3
                with pytest.raises(CatalogVersionConflict):
                    await publish(tx, old, published_by="integration")
                assert (await load_catalog(tx, old_version)).lock == lock  # savepoint preserves transaction
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("table", ["tool_catalog_versions", "tool_definitions"])
@pytest.mark.parametrize("verb", ["UPDATE", "DELETE", "TRUNCATE"])
def test_direct_mutation_of_published_definitions_is_rejected(table, verb):
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        version = uuid.uuid4().hex
        try:
            async with factory.begin() as tx:
                await publish(tx, build_catalog(Catalog.load(None), version), published_by="integration")
            column = "version" if table == "tool_catalog_versions" else "catalog_version"
            sql = (f"UPDATE {table} SET content_hash=:value WHERE {column}=:version" if verb == "UPDATE" else
                   f"DELETE FROM {table} WHERE {column}=:version" if verb == "DELETE" else
                   f"TRUNCATE {table} CASCADE")
            with pytest.raises(DBAPIError):
                async with factory.begin() as tx:
                    await tx.execute(text(sql), {"version": version, "value": "0" * 64})
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_publication_rollback_and_concurrent_version_conflict():
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        c = build_catalog(Catalog.load(None), uuid.uuid4().hex)
        try:
            with pytest.raises(RuntimeError):
                async with factory.begin() as tx:
                    await publish(tx, c, published_by="integration")
                    raise RuntimeError("crash before commit")
            async with factory() as tx:
                assert await tx.scalar(text("SELECT count(*) FROM tool_definitions WHERE catalog_version=:v"), {"v": c.lock.version}) == 0
            async def write():
                try:
                    async with factory.begin() as tx:
                        await publish(tx, c, published_by="integration")
                    return "published"
                except CatalogVersionConflict:
                    return "conflict"
            assert sorted(await asyncio.gather(write(), write())) == ["conflict", "published"]
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_definition_insert_must_match_sealed_snapshot():
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        version = uuid.uuid4().hex
        try:
            async with factory.begin() as tx:
                await publish(tx, ToolCatalog(CatalogSnapshot(version=version, tools=[])), published_by="integration")
            with pytest.raises(DBAPIError):
                async with factory.begin() as tx:
                    await tx.execute(text("""INSERT INTO tool_definitions
                        (catalog_version,name,tool_version,content_hash,definition)
                        VALUES (:version,'forged','1',:hash,'{}')"""), {"version": version, "hash": "a" * 64})
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_unknown_provider_error_uses_existing_durable_alerts():
    from backend.core.tools.errors import normalize_error_durable
    async def scenario():
        engine = create_async_database_engine(DatabaseSettings(url=DB))
        factory = create_async_session_factory(engine)
        task_id = uuid.uuid4().hex
        try:
            async with factory.begin() as tx:
                result = await normalize_error_durable(tx, "unrecognized", "novel", task_id=task_id, run_id="run")
                assert result.kind == "unknown"
            async with factory() as tx:
                count = await tx.scalar(text("SELECT count(*) FROM core_event_alerts WHERE kind='unknown_tool_failure' AND context->>'task_id'=:task"), {"task": task_id})
                assert count == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())
