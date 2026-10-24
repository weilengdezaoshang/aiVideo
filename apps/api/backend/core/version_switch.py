"""Persist a version switch and apply it only inside the worker. No generation or credits.

Daemon hook (Codex, do not import from production.py here):
  worker = await install(session_factory)
  applied = await worker.run_once()
Recovery uses FOR UPDATE SKIP LOCKED. Notifications are core.changed with documentId and commandId.
API hook: mount build_switch_router(); it uses Depends(authenticated) and app.state.factory.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.asset_history import asset_head, version_projection
from backend.core.orm import CoreAssetVersion
from backend.core.version_switch_orm import VersionSwitchCommand
from backend.infrastructure.orm import AssetReference, Document, Outbox
from backend.services.identity import Identity, authenticated

DAEMON_HOOK = (
    "Call VersionSwitchWorker.run_once from a new daemon loop. Recovery SQL is RECOVERY_SQL. "
    "Do not edit workers/daemon.py in this change."
)
API_HOOK = (
    "Mount build_switch_router(). It depends on authenticated Identity and app.state.factory. "
    "The route does not write canvas data."
)

RECOVERY_SQL = """
SELECT id FROM core_version_switch_commands
WHERE status = 'leased' AND lease_until IS NOT NULL AND lease_until < clock_timestamp()
FOR UPDATE SKIP LOCKED
"""


class SwitchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_VERSION_SWITCH_", frozen=True, extra="forbid")
    batch: int = Field(default=8, gt=0, le=100)
    lease_seconds: float = Field(default=30, gt=0)
    backoff_seconds: float = Field(default=1, gt=0)


class SwitchRejected(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SwitchCommand(BaseModel):
    document_id: UUID
    node_id: str = Field(min_length=1, max_length=128)
    expected_document_revision: int = Field(ge=0)
    expected_anchor_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)


def request_hash(asset_id: UUID, version_id: UUID, body: SwitchCommand, *, user_id: str) -> str:
    payload = {"user_id": user_id, "asset_id": str(asset_id), "version_id": str(version_id),
               **body.model_dump(mode="json")}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _notify(tx, command: VersionSwitchCommand) -> Outbox:
    row = Outbox(aggregate_type="document", aggregate_id=str(command.document_id), event_type="core.changed",
                 payload={"documentId": str(command.document_id), "commandId": str(command.id),
                          "workspaceId": str(command.workspace_id)})
    tx.add(row)
    return row


async def _db_now(tx):
    return await tx.scalar(select(func.clock_timestamp()))


def apply_switch_canvas(data: dict, *, node_id: str, stored_asset_id: UUID, anchor_version: int,
                        chain_asset_ids: set[str]) -> dict:
    """Pure document update. Caller has already locked the row and checked revision."""
    objects = dict(data.get("objects") or {})
    node = objects.get(node_id)
    if not isinstance(node, dict):
        raise SwitchRejected("node_not_found")
    current = str(node.get("assetId") or "")
    if current not in chain_asset_ids:
        raise SwitchRejected("node_not_in_asset_chain")
    if int(node.get("assetVersion") or 0) != anchor_version:
        raise SwitchRejected("anchor_cas_conflict")
    objects[node_id] = {
        **node,
        "assetId": str(stored_asset_id),
        "src": f"/api/assets/{stored_asset_id}/content",
        "assetVersion": anchor_version + 1,
    }
    return {**data, "objects": objects}


async def _chain_ids(tx, asset_id: UUID) -> set[str]:
    rows = list(await tx.scalars(select(CoreAssetVersion).where(CoreAssetVersion.asset_id == asset_id)))
    return {str(row.stored_asset_id) for row in rows if row.stored_asset_id}


async def _version_ok(tx, *, asset_id: UUID, version_id: UUID, workspace_id: UUID, user_id: str) -> CoreAssetVersion:
    try:
        await asset_head(tx, asset_id=asset_id, workspace_id=workspace_id, user_id=user_id)
    except LookupError as exc:
        raise SwitchRejected("asset_not_authorized") from exc
    row = await tx.get(CoreAssetVersion, version_id)
    if row is None or row.asset_id != asset_id or row.workspace_id != workspace_id:
        raise SwitchRejected("asset_version_not_found")
    if row.status != "normal":
        raise SwitchRejected("asset_version_not_normal")
    view = await version_projection(tx, row)
    if view["src"] is None:
        raise SwitchRejected("source_host_blocked_or_unreadable")
    return row


async def submit_switch(tx: AsyncSession, *, asset_id: UUID, version_id: UUID, workspace_id: UUID,
                        user_id: str, body: SwitchCommand) -> VersionSwitchCommand:
    digest = request_hash(asset_id, version_id, body, user_id=user_id)
    existing = await _owned_command(tx, workspace_id, user_id, body.idempotency_key)
    if existing is not None:
        if existing.request_hash != digest:
            raise SwitchRejected("idempotency_conflict")
        return existing
    document = await tx.get(Document, body.document_id)
    if document is None or document.workspace_id != workspace_id:
        raise SwitchRejected("document_not_found")
    if document.revision != body.expected_document_revision:
        raise SwitchRejected("document_cas_conflict")
    row = await _version_ok(tx, asset_id=asset_id, version_id=version_id, workspace_id=workspace_id, user_id=user_id)
    chain = await _chain_ids(tx, asset_id)
    node = (document.data.get("objects") or {}).get(body.node_id)
    if not isinstance(node, dict) or str(node.get("assetId") or "") not in chain:
        raise SwitchRejected("node_not_in_asset_chain")
    if int(node.get("assetVersion") or 0) != body.expected_anchor_version:
        raise SwitchRejected("anchor_cas_conflict")
    command = VersionSwitchCommand(
        workspace_id=workspace_id, user_id=user_id, document_id=document.id, node_id=body.node_id,
        core_asset_id=asset_id, core_asset_version_id=row.id,
        expected_document_revision=body.expected_document_revision,
        expected_anchor_version=body.expected_anchor_version,
        idempotency_key=body.idempotency_key, request_hash=digest, status="pending")
    try:
        async with tx.begin_nested():
            tx.add(command)
            await tx.flush()
            outbox = _notify(tx, command)
            await tx.flush()
            command.outbox_id = outbox.id
    except IntegrityError:
        tx.expunge(command)
        raced = await _owned_command(tx, workspace_id, user_id, body.idempotency_key)
        if raced is None or raced.request_hash != digest:
            raise SwitchRejected("idempotency_conflict")
        return raced
    return command


async def _owned_command(tx, workspace_id, user_id, key):
    return await tx.scalar(select(VersionSwitchCommand).where(
        VersionSwitchCommand.workspace_id == workspace_id,
        VersionSwitchCommand.user_id == user_id,
        VersionSwitchCommand.idempotency_key == key))


class VersionSwitchWorker:
    def __init__(self, factory, settings: SwitchSettings | None = None, owner: str = "version-switch"):
        self.factory, self.settings, self.owner = factory, settings or SwitchSettings(), owner

    async def run_once(self) -> int:
        async with self.factory.begin() as tx:
            now = await _db_now(tx)
            expired = list(await tx.scalars(select(VersionSwitchCommand).where(
                VersionSwitchCommand.status == "leased",
                VersionSwitchCommand.lease_until.is_not(None),
                VersionSwitchCommand.lease_until < func.clock_timestamp(),
            ).limit(self.settings.batch).with_for_update(skip_locked=True)))
            for command in expired:
                command.status = "pending"
                command.lease_owner = None
                command.lease_token = None
                command.lease_until = None
                command.next_at = now
            due = list(await tx.scalars(select(VersionSwitchCommand).where(
                VersionSwitchCommand.status == "pending",
                VersionSwitchCommand.next_at <= func.clock_timestamp(),
            ).limit(self.settings.batch).with_for_update(skip_locked=True)))
            claimed = []
            for command in due:
                token = uuid4()
                command.status = "leased"
                command.lease_token = token
                command.lease_owner = str(token)
                command.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
                claimed.append((command.id, token))
            await tx.flush()
        applied = 0
        for ident, token in claimed:
            if await self._apply(ident, token):
                applied += 1
        return applied

    async def _apply(self, command_id: UUID, token: UUID) -> bool:
        async with self.factory.begin() as tx:
            command = await tx.scalar(select(VersionSwitchCommand).where(
                VersionSwitchCommand.id == command_id).with_for_update(skip_locked=True))
            if command is None:
                return False
            now = await _db_now(tx)
            if (command.status != "leased" or command.lease_token != token
                    or command.lease_until is None or command.lease_until <= now):
                return False
            try:
                document = await tx.scalar(select(Document).where(
                    Document.id == command.document_id).with_for_update(skip_locked=True))
                if document is None:
                    present = await tx.scalar(select(Document.id).where(Document.id == command.document_id))
                    if present is None:
                        raise SwitchRejected("document_not_found")
                    await self._reschedule(tx, command, now)
                    return False
                if document.workspace_id != command.workspace_id:
                    raise SwitchRejected("document_not_found")
                row = await _version_ok(tx, asset_id=command.core_asset_id, version_id=command.core_asset_version_id,
                                        workspace_id=command.workspace_id, user_id=command.user_id)
                if document.revision != command.expected_document_revision:
                    raise SwitchRejected("document_cas_conflict")
                if row.stored_asset_id is None:
                    raise SwitchRejected("source_host_blocked_or_unreadable")
                chain = await _chain_ids(tx, command.core_asset_id)
                new_data = apply_switch_canvas(
                    document.data, node_id=command.node_id, stored_asset_id=row.stored_asset_id,
                    anchor_version=command.expected_anchor_version, chain_asset_ids=chain)
                changed = await tx.execute(update(Document).where(
                    Document.id == document.id, Document.revision == command.expected_document_revision,
                ).values(data=new_data, revision=command.expected_document_revision + 1))
                if changed.rowcount != 1:
                    raise SwitchRejected("document_cas_conflict")
                existing_ref = await tx.scalar(select(AssetReference).where(
                    AssetReference.asset_id == row.stored_asset_id, AssetReference.owner_type == "document",
                    AssetReference.owner_id == str(document.id)))
                if existing_ref is None:
                    tx.add(AssetReference(asset_id=row.stored_asset_id, owner_type="document", owner_id=str(document.id)))
                command.status = "applied"
                command.error_code = None
                command.result = {"document_revision": command.expected_document_revision + 1,
                                  "asset_version": command.expected_anchor_version + 1}
            except SwitchRejected as exc:
                command.status = "conflict" if exc.code.endswith("conflict") else "rejected"
                command.error_code = exc.code
            command.lease_owner = None
            command.lease_token = None
            command.lease_until = None
            note = _notify(tx, command)
            await tx.flush()
            command.outbox_id = note.id
            return command.status == "applied"

    async def _reschedule(self, tx, command, now):
        command.status = "pending"
        command.lease_owner = None
        command.lease_token = None
        command.lease_until = None
        command.attempt += 1
        command.next_at = now + timedelta(seconds=self.settings.backoff_seconds)
        _notify(tx, command)


async def install(factory, settings: SwitchSettings | None = None) -> VersionSwitchWorker:
    """Fail closed when migration 0025 is absent. Does not start a daemon."""
    async with factory.begin() as tx:
        await tx.execute(text("SELECT 1 FROM core_version_switch_commands LIMIT 0"))
    return VersionSwitchWorker(factory, settings)


def build_switch_router():
    router = APIRouter()

    @router.post("/api/core/assets/{asset_id}/versions/{version_id}/switch")
    async def switch(asset_id: UUID, version_id: UUID, body: SwitchCommand, request: Request,
                     identity: Identity = Depends(authenticated)):
        factory = request.app.state.factory
        try:
            async with factory.begin() as tx:
                command = await submit_switch(
                    tx, asset_id=asset_id, version_id=version_id, workspace_id=identity.workspace_id,
                    user_id=identity.principal, body=body)
        except SwitchRejected as exc:
            raise HTTPException(409, exc.code) from exc
        return {"command_id": str(command.id), "outbox_id": str(command.outbox_id) if command.outbox_id else None,
                "status": command.status}

    return router
