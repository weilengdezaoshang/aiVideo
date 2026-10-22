"""Transaction-scoped publication; no latest fallback, commit, or model calls."""
import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .catalog import CatalogError, ToolCatalog, canonical
from .contracts import CatalogSnapshot, VersionLock
from .orm import ToolCatalogVersion, ToolDefinitionRecord


class CatalogVersionConflict(CatalogError):
    pass


async def publish(tx: AsyncSession, catalog: ToolCatalog, *, published_by: str) -> VersionLock:
    lock = catalog.lock
    snapshot = catalog.snapshot.model_dump(mode="json")
    if not published_by or len(published_by) > 200:
        raise CatalogError("publisher_required")
    try:
        async with tx.begin_nested():
            tx.add(ToolCatalogVersion(version=lock.version, content_hash=lock.content_hash,
                schema_version=1, published_by=published_by, snapshot=snapshot))
            await tx.flush()
            for tool in snapshot["tools"]:
                tx.add(ToolDefinitionRecord(catalog_version=lock.version, name=tool["name"],
                    tool_version=tool["version"], definition=tool,
                    content_hash=hashlib.sha256(canonical(tool).encode()).hexdigest()))
            await tx.flush()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise CatalogVersionConflict("catalog_version_already_published") from exc
        raise
    return lock


async def load_catalog(tx: AsyncSession, version: str, *, expected_lock: VersionLock | None = None) -> ToolCatalog:
    row = await tx.get(ToolCatalogVersion, version, populate_existing=True)
    if row is None:
        raise CatalogError("catalog_version_not_found")
    catalog = ToolCatalog(CatalogSnapshot.model_validate(row.snapshot))
    if catalog.lock.content_hash != row.content_hash:
        raise CatalogError("catalog_integrity_error")
    rows = (await tx.scalars(select(ToolDefinitionRecord).where(
        ToolDefinitionRecord.catalog_version == version))).all()
    indexed = {r.name: r for r in rows}
    if set(indexed) != {t.name for t in catalog.snapshot.tools}:
        raise CatalogError("catalog_definition_index_mismatch")
    for tool in catalog.snapshot.tools:
        record = indexed[tool.name]
        encoded = canonical(tool.model_dump(mode="json"))
        if (canonical(record.definition) != encoded or record.tool_version != tool.version
                or record.content_hash != hashlib.sha256(encoded.encode()).hexdigest()):
            raise CatalogError("catalog_definition_integrity_error")
    if expected_lock is not None:
        catalog.check_lock(expected_lock)
    return catalog


async def get_tool(tx: AsyncSession, name: str, version: str):
    return (await load_catalog(tx, version)).get_tool(name, version)
