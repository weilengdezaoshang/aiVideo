"""Asset-card domain over the existing core asset version chain.

A card is a core asset with category="card". Every modification (content,
star, status) appends a version through append_asset_version so a running Run
keeps its frozen view; active runs that froze the card receive an asset_changed
event. Card writes happen only through this module (HTTP or the worker's card
tasks) — never as a model-side effect.
"""
from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select

from .contracts import AssetContent
from .events.store import emit
from .orm import CoreAsset, CoreAssetLineage, CoreAssetVersion, CoreEvent, CoreRun, CoreTask
from .states import EntityNotFound, VersionConflict
from .store import append_asset_version, create_asset

CARD_KINDS = ("subject", "style", "character", "environment")
RUN_TERMINAL = ("completed", "failed", "cancelled")
NAME_MAX = 120
DESCRIPTION_MAX = 1000
RECOGNITION_FIELD_MAX = 500


class CardRejected(Exception):
    """Program-side card validation failure; maps to a failed tool task."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _text_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def card_content(*, card_kind: str, name: str, description: str = "", starred: bool = False,
                 storage: dict, stored_asset_id: uuid.UUID | None = None, content_hash: str,
                 user_id: str, recognition: dict | None = None, layers: dict | None = None,
                 status: str = "normal", kind: str = "text", width: int | None = None,
                 height: int | None = None, size_bytes: int | None = None,
                 run_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None) -> AssetContent:
    if card_kind not in CARD_KINDS:
        raise CardRejected("invalid_card_kind")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= NAME_MAX:
        raise CardRejected("invalid_card_name")
    if not isinstance(description, str) or len(description) > DESCRIPTION_MAX:
        raise CardRejected("invalid_card_description")
    return AssetContent(kind=kind, category="card", card_kind=card_kind, starred=starred,
        recognition=recognition or {}, layers=layers or {},
        metadata_json={"name": name.strip(), "description": description},
        storage=storage, stored_asset_id=stored_asset_id, content_hash=content_hash,
        width=width, height=height, size_bytes=size_bytes, status=status,
        run_id=run_id, task_id=task_id, user_id=user_id)


async def create_card(tx, *, workspace_id: uuid.UUID, user_id: str, card_kind: str, name: str,
                      description: str = "", source_stored_asset=None, starred: bool = False,
                      run_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None,
                      notify: bool = True) -> tuple[CoreAsset, CoreAssetVersion]:
    """Create a card; a source image becomes the card face and its lineage parent."""
    from backend.infrastructure.orm import Asset
    stored: Asset | None = source_stored_asset
    if stored is not None and (not isinstance(stored, Asset) or stored.workspace_id != workspace_id):
        raise CardRejected("card_source_unavailable")
    content = (card_content(card_kind=card_kind, name=name, description=description, starred=starred,
        storage={"asset_id": str(stored.id)} if stored is not None else {},
        stored_asset_id=stored.id if stored is not None else None,
        content_hash=stored.sha256 if stored is not None else _text_hash(card_kind, name, description),
        user_id=user_id, kind=stored.kind if stored is not None else "text",
        width=stored.width if stored is not None else None,
        height=stored.height if stored is not None else None,
        size_bytes=stored.bytes if stored is not None else None, run_id=run_id, task_id=task_id))
    card = await create_asset(tx, workspace_id, user_id, "personal", content)
    version = await tx.get(CoreAssetVersion, card.current_version_id)
    if stored is not None:
        parent = await tx.scalar(select(CoreAssetVersion.id).where(
            CoreAssetVersion.stored_asset_id == stored.id,
            CoreAssetVersion.category == "artifact",
            CoreAssetVersion.workspace_id == workspace_id).order_by(CoreAssetVersion.created_at.desc()).limit(1))
        if parent is not None:
            tx.add(CoreAssetLineage(input_version_id=parent, output_version_id=version.id, relation="card_of"))
    if notify:
        await notify_card_changed(tx, card.id, exclude_run_id=run_id)
    return card, version


async def load_card(tx, card_id, workspace_id: uuid.UUID) -> tuple[CoreAsset, CoreAssetVersion]:
    card = await tx.get(CoreAsset, card_id)
    if card is None or card.workspace_id != workspace_id:
        raise EntityNotFound("card_not_found")
    version = await tx.get(CoreAssetVersion, card.current_version_id)
    if version is None:
        raise EntityNotFound("card_not_found")
    return card, version


async def _append_card_version(tx, card_id: uuid.UUID, relation: str, build) -> CoreAssetVersion:
    """Append with CAS; on conflict rebuild from the freshly committed head.

    ``build(head)`` produces the new content so a concurrent edit's fields
    survive the retry instead of being overwritten by a stale snapshot.
    """
    for attempt in (0, 1):
        card = await tx.scalar(select(CoreAsset).where(CoreAsset.id == card_id)
            .execution_options(populate_existing=True))
        if card is None:
            raise EntityNotFound("card_not_found")
        head = await tx.get(CoreAssetVersion, card.current_version_id, populate_existing=True)
        try:
            return await append_asset_version(tx, card.id, card.version, build(head),
                parents=(head.id,), relation=relation)
        except VersionConflict:
            if attempt:
                raise


def _head_content(head: CoreAssetVersion, *, metadata: dict | None = None, starred: bool | None = None,
                  status: str | None = None, run_id=None, task_id=None) -> AssetContent:
    """Rebuild a full version snapshot from a head plus explicit overrides."""
    return AssetContent(kind=head.kind, category=head.category, card_kind=head.card_kind,
        starred=head.starred if starred is None else starred, recognition=head.recognition or {},
        layers=head.layers or {}, metadata_json=head.metadata_json if metadata is None else metadata,
        storage=head.storage or {}, stored_asset_id=head.stored_asset_id,
        content_hash=head.content_hash, mime_type=head.mime_type, size_bytes=head.size_bytes,
        width=head.width, height=head.height, duration_ms=head.duration_ms,
        status=head.status if status is None else status,
        run_id=head.run_id if run_id is None else run_id,
        task_id=head.task_id if task_id is None else task_id, user_id=head.user_id)


async def update_card(tx, card_id: uuid.UUID, workspace_id: uuid.UUID, expected_version_number: int,
                      *, name: str | None = None, description: str | None = None,
                      starred: bool | None = None, run_id: uuid.UUID | None = None,
                      task_id: uuid.UUID | None = None, notify: bool = True) -> CoreAssetVersion:
    """Any card edit appends a version; only normal-state cards are editable."""
    card, head = await load_card(tx, card_id, workspace_id)
    if head.version != expected_version_number:
        raise CardRejected("card_version_conflict")
    if head.status != "normal":
        # report/remove own the status channel; an edit must not resurrect a delisted card.
        raise CardRejected("card_not_editable")
    if name is None and description is None and starred is None:
        raise CardRejected("card_update_empty")

    def build(head: CoreAssetVersion) -> AssetContent:
        metadata = dict(head.metadata_json or {})
        if name is not None:
            metadata["name"] = name
        if description is not None:
            metadata["description"] = description
        return _head_content(head, metadata=metadata,
            starred=head.starred if starred is None else bool(starred),
            run_id=run_id, task_id=task_id)

    version = await _append_card_version(tx, card.id, "edited", build)
    if notify:
        await notify_card_changed(tx, card_id, exclude_run_id=run_id)
    return version


async def set_card_status(tx, card_id: uuid.UUID, workspace_id: uuid.UUID, expected_version_number: int,
                          *, status: str, notify: bool = True) -> CoreAssetVersion:
    if status not in ("reported", "removed"):
        raise CardRejected("invalid_card_status")
    card, head = await load_card(tx, card_id, workspace_id)
    if head.version != expected_version_number:
        raise CardRejected("card_version_conflict")
    if head.status == status:
        raise CardRejected("card_status_unchanged")
    version = await _append_card_version(tx, card.id, "edited",
        lambda head: _head_content(head, status=status))
    if notify:
        await notify_card_changed(tx, card_id)
    return version


def slim_recognition(recognition: dict | None) -> dict:
    """Bounded recognition view for model context; never forwards mask payloads."""
    raw = recognition if isinstance(recognition, dict) else {}
    subject = raw.get("subject") if isinstance(raw.get("subject"), dict) else {}
    style = raw.get("style") if isinstance(raw.get("style"), dict) else {}
    tags = [str(tag)[:32] for tag in (style.get("tags") or []) if str(tag).strip()][:12]
    return {
        "subject": {"category": str(subject.get("category") or "")[:64],
                    "description": str(subject.get("description") or "")[:RECOGNITION_FIELD_MAX]},
        "style": {"tags": tags,
                  "description": str(style.get("description") or "")[:RECOGNITION_FIELD_MAX]},
        "overall": str(raw.get("overall") or "")[:RECOGNITION_FIELD_MAX],
    }


def version_view(card: CoreAsset, version: CoreAssetVersion, *, head_status: str | None = None) -> dict:
    metadata = version.metadata_json or {}
    storage = version.storage or {}
    return {
        "card_id": str(card.id), "asset_id": str(card.id), "version_id": str(version.id),
        "version_number": version.version, "head_version": card.version,
        "card_kind": version.card_kind, "category": version.category, "kind": version.kind,
        "name": str(metadata.get("name") or ""), "description": str(metadata.get("description") or ""),
        "starred": bool(version.starred), "source_asset_id": str(storage.get("asset_id") or "") or None,
        "recognition": slim_recognition(version.recognition), "status": version.status,
        "head_status": head_status or version.status,
        "ownership": card.ownership,
    }


async def frozen_card_views(tx, run: CoreRun, *, limit: int) -> list[dict]:
    """Frozen card views for the supervisor context; delisted cards disappear."""
    lock = (run.version_lock or {}).get("asset_cards") or {}
    views = []
    for card_id, version_id in list(lock.items())[:limit]:
        card = await tx.get(CoreAsset, uuid.UUID(str(card_id)))
        if card is None or card.workspace_id != run.workspace_id:
            continue
        head = await tx.get(CoreAssetVersion, card.current_version_id)
        version = await tx.get(CoreAssetVersion, uuid.UUID(str(version_id)))
        if head is None or version is None or version.asset_id != card.id:
            continue
        if head.status != "normal":
            continue
        views.append(version_view(card, version, head_status=head.status))
    return views


async def resolve_card(tx, run: CoreRun, card_id, *, allow_run_created: bool = False) -> dict:
    """Frozen-version card for the worker: head status gates new references.

    Cards created by this Run's own create_asset_card tasks have no frozen
    version; with ``allow_run_created`` the current head is the run's view.
    """
    key = str(card_id)
    try:
        ident = uuid.UUID(key)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("card_out_of_scope") from None
    lock = (run.version_lock or {}).get("asset_cards") or {}
    card = await tx.get(CoreAsset, ident)
    if card is None or card.workspace_id != run.workspace_id:
        raise ValueError("card_out_of_scope")
    head = await tx.get(CoreAssetVersion, card.current_version_id)
    if head is None:
        raise ValueError("card_out_of_scope")
    version = None
    if key in lock:
        version = await tx.get(CoreAssetVersion, uuid.UUID(str(lock[key])))
        if version is None or version.asset_id != card.id:
            raise ValueError("card_out_of_scope")
    elif not allow_run_created:
        raise ValueError("card_out_of_scope")
    else:
        version = head
    if head.status != "normal":
        raise ValueError("card_unavailable")
    view = version_view(card, version, head_status=head.status)
    source = None
    if view["source_asset_id"]:
        from backend.infrastructure.orm import Asset
        stored = await tx.get(Asset, uuid.UUID(view["source_asset_id"]))
        if stored is not None and stored.workspace_id == run.workspace_id:
            source = {"asset_id": str(stored.id), "storage_key": stored.storage_key,
                      "sha256": stored.sha256, "ext": stored.ext, "kind": stored.kind,
                      "width": stored.width, "height": stored.height, "media_type": stored.kind}
    view["source"] = source
    return view


async def run_created_card_ids(tx, run: CoreRun) -> set[str]:
    """Cards this Run created itself; only a succeeded create task counts."""
    rows = await tx.scalars(select(CoreTask).where(CoreTask.run_id == run.id,
        CoreTask.tool_ref == "create_asset_card", CoreTask.status == "succeeded"))
    return {str((row.output or {}).get("card_id") or "") for row in rows} - {""}


def card_prompt_text(card: dict, usage: str) -> str:
    """Deterministic text merged into the resolved prompt; audit lives in resolved_parameters."""
    name = str(card.get("name") or "").strip()
    recognition = card.get("recognition") or {}
    subject = recognition.get("subject") or {}
    style = recognition.get("style") or {}
    parts: list[str] = []
    if usage == "style":
        tags = "、".join(style.get("tags") or [])
        head = f"风格参考「{name}」" + (f"（{tags}）" if tags else "")
        body = str(style.get("description") or card.get("description") or "")
    else:
        category = str(subject.get("category") or "").strip()
        head = f"主体参考「{name}」" + (f"（{category}）" if category else "")
        body = str(subject.get("description") or card.get("description") or "")
    parts.append(head)
    if body:
        parts.append(body[:RECOGNITION_FIELD_MAX])
    overall = str(recognition.get("overall") or "").strip()
    if overall and usage == "subject":
        parts.append(overall[:RECOGNITION_FIELD_MAX])
    return "：".join(parts) if len(parts) > 1 else parts[0] if parts else ""


def apply_card_parameters(tool, params: dict) -> dict:
    """Convert resolved card references per the tool's recorded capability.

    V1 supports prompt_text only: card descriptions merge into the prompt and the
    applied references are recorded in ``_card_refs`` for audit. Unsupported or
    malformed references fail closed with param_invalid.
    """
    support = (tool.limits or {}).get("card_support") or {}
    resolved = dict(params)
    used: list[dict] = []
    additions: list[str] = []
    for key, usage in (("style_cards", "style"), ("subject_cards", "subject")):
        refs = resolved.pop(key, None) or []
        if not isinstance(refs, list):
            raise ValueError("invalid_card_reference")
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("media_type") != "card" or not ref.get("card_id"):
                raise ValueError("invalid_card_reference")
            if not support.get("prompt_text"):
                raise ValueError("card_unsupported_by_tool")
            text = card_prompt_text(ref, usage)
            if text:
                additions.append(text)
            used.append({"card_id": str(ref["card_id"]), "usage": usage,
                         "version_id": str(ref.get("version_id") or "")})
    if additions:
        prompt = str(resolved.get("prompt") or "")
        resolved["prompt"] = (prompt + "\n" if prompt else "") + "\n".join(additions)
    if used:
        resolved["_card_refs"] = used
    return resolved


async def notify_card_changed(tx, card_id: uuid.UUID, *, exclude_run_id: uuid.UUID | None = None) -> int:
    """Emit asset_changed into every active run that froze this card.

    The run whose own card task made the change is excluded: its task_succeeded
    event already reports the outcome.
    """
    card = await tx.get(CoreAsset, card_id)
    if card is None:
        return 0
    runs = (await tx.scalars(select(CoreRun).where(CoreRun.workspace_id == card.workspace_id,
        CoreRun.status.not_in(RUN_TERMINAL))))
    sent = 0
    # Deterministic order keeps concurrent card updates from deadlocking on run locks.
    for run in sorted(runs, key=lambda row: str(row.id)):
        if exclude_run_id is not None and run.id == exclude_run_id:
            continue
        lock = (run.version_lock or {}).get("asset_cards") or {}
        if str(card_id) not in lock:
            continue
        cause = await tx.scalar(select(CoreEvent.id).where(CoreEvent.run_id == run.id)
            .order_by(CoreEvent.run_sequence.desc()).limit(1))
        if cause is None:
            continue
        await emit(tx, "asset_changed", run.id, causation_id=cause, ids={"asset_id": card_id})
        sent += 1
    return sent


async def apply_card_task(tx, task, execution, run: CoreRun) -> dict:
    """Worker-side card task effect; runs inside the consume transaction."""
    params = execution.resolved_parameters or task.parameters or {}
    if task.tool_ref == "create_asset_card":
        from backend.infrastructure.orm import Asset
        source = params.get("image") or {}
        stored = None
        if source.get("asset_id"):
            stored = await tx.get(Asset, uuid.UUID(str(source["asset_id"])))
            if stored is None or stored.workspace_id != run.workspace_id:
                raise CardRejected("card_source_unavailable")
        card, version = await create_card(tx, workspace_id=run.workspace_id, user_id=run.user_id,
            card_kind=str(params.get("card_kind") or ""), name=str(params.get("name") or ""),
            description=str(params.get("description") or ""), source_stored_asset=stored,
            starred=bool(params.get("starred")), run_id=run.id, task_id=task.id)
        return {"card_id": str(card.id), "media_type": "card", "version_id": str(version.id),
                "version_number": version.version}
    if task.tool_ref == "update_asset_card":
        try:
            card_id = uuid.UUID(str(params.get("card_id") or ""))
        except (ValueError, TypeError, AttributeError):
            raise CardRejected("card_out_of_scope") from None
        frozen = (run.version_lock or {}).get("asset_cards") or {}
        if str(card_id) not in frozen and str(card_id) not in await run_created_card_ids(tx, run):
            raise CardRejected("card_out_of_scope")
        expected = params.get("expected_version_number")
        if type(expected) is not int or expected < 1:
            raise CardRejected("card_version_conflict")
        version = await update_card(tx, card_id, run.workspace_id, expected,
            name=params.get("name"), description=params.get("description"),
            starred=params.get("starred"), run_id=run.id, task_id=task.id)
        return {"card_id": str(card_id), "media_type": "card", "version_id": str(version.id),
                "version_number": version.version}
    raise CardRejected("unknown_card_tool")
