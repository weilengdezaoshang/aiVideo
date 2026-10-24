"""Database-backed conversation references and confirmed decisions, never model geometry."""

from uuid import UUID
import math
import copy
from types import SimpleNamespace

from sqlalchemy import String, cast, func, select

from backend.core.orm import (CoreAsset, CoreAssetVersion, CoreRun, CoreRunInputRevision, CoreSessionArtifact,
    CoreSessionDecision, CoreTask)
from backend.core.session_reference_intent import reference_intent
from backend.core.session_references import ArtifactCandidate, ReferenceQuery, ReferenceSettings, resolve
from backend.infrastructure.orm import Asset, AssetReference, Document


async def readable_asset(tx, session, ident: str):
    try:
        asset_id = UUID(ident)
    except (ValueError, TypeError):
        raise ValueError("asset_unavailable") from None
    asset = await tx.get(Asset, asset_id)
    if asset is None or asset.workspace_id != session.workspace_id or (asset.details or {}).get("hostBlocked"):
        raise ValueError("asset_unavailable")
    version = await tx.scalar(select(CoreAssetVersion).where(
        CoreAssetVersion.stored_asset_id == asset_id).order_by(CoreAssetVersion.version.desc()).limit(1))
    if version is not None:
        head = await tx.get(CoreAsset, version.asset_id)
        if (head is None or head.workspace_id != session.workspace_id or version.workspace_id != session.workspace_id or version.status != "normal" or
                (head.ownership != "team" and head.user_id != session.user_id)):
            raise ValueError("asset_unavailable")
    else:
        reference = await tx.scalar(select(AssetReference.id).where(AssetReference.asset_id == asset_id,
            AssetReference.owner_type == "document", AssetReference.owner_id == str(session.canvas_id)))
        if reference is None:
            reference = await tx.scalar(select(AssetReference.id).join(CoreRun,
                cast(CoreRun.id, String) == AssetReference.owner_id).where(
                    AssetReference.asset_id == asset_id, AssetReference.owner_type == "core_run",
                    CoreRun.session_id == session.id, CoreRun.workspace_id == session.workspace_id,
                    CoreRun.user_id == session.user_id))
        if reference is None:
            raise ValueError("asset_unavailable")
    return asset


async def reference_context(tx, session, request: dict, settings: ReferenceSettings | None = None):
    """Resolve explicit text against this Session, then validate the chosen stored asset.

    Returns a new request and a resolution. Ambiguity stays explicit for a program
    clarification; it must never be passed as a guessed selection to the planner.
    """
    settings = settings or ReferenceSettings()
    copied = dict(request)
    copied.pop("reference_clarification", None)
    selected = {str(value) for value in request.get("selected_assets") or []}
    if len(selected) > settings.candidate_limit:
        copied["selected_assets"] = []
        return copied, {"status": "ambiguous", "reason": "candidate_limit", "candidate_ids": []}
    explicit = request.get("selection_explicit", True)
    if len(selected) == 1 and settings.prefer_selection and explicit:
        ident = next(iter(selected))
        try:
            await readable_asset(tx, session, ident)
        except ValueError:
            copied["selected_assets"] = []
            return copied, {"status": "unavailable", "reason": "asset_unavailable", "candidate_ids": []}
        return copied, {"status": "resolved", "reason": "selected", "asset_id": ident, "artifact_id": None}
    texts = request.get("messages")
    texts = texts if isinstance(texts, list) else [request.get("message") or ""]
    query = None
    try:
        for text in reversed(texts):
            query = reference_intent(str(text), session_id=str(session.id), batch_id=request.get("reference_batch_id"))
            if query is not None:
                break
    except ValueError:
        return copied, {"status": "ambiguous", "reason": "ambiguous_reference_intent", "candidate_ids": []}
    if query is None:
        for ident in selected:
            try:
                await readable_asset(tx, session, ident)
            except ValueError:
                copied["selected_assets"] = []
                return copied, {"status": "unavailable", "reason": "asset_unavailable", "candidate_ids": []}
        return copied, None
    if selected and explicit and (settings.prefer_selection or query["kind"] == "selected"):
        for ident in selected:
            try:
                await readable_asset(tx, session, ident)
            except ValueError:
                copied["selected_assets"] = []
                return copied, {"status": "unavailable", "reason": "asset_unavailable", "candidate_ids": []}
        if len(selected) != 1:
            return copied, {"status": "ambiguous", "reason": "multiple_selected", "candidate_ids": []}
        return copied, {"status": "resolved", "reason": "selected", "asset_id": next(iter(selected)), "artifact_id": None}
    doc = await tx.get(Document, session.canvas_id) if session.canvas_id else None
    objects = (doc.data or {}).get("objects", {}) if doc and doc.workspace_id == session.workspace_id else {}
    rows = list(await tx.execute(select(CoreSessionArtifact, CoreTask,
        func.row_number().over(partition_by=CoreTask.plan_id, order_by=CoreSessionArtifact.ordinal))
        .join(CoreTask, CoreTask.id == CoreSessionArtifact.task_id)
        .where(CoreSessionArtifact.session_id == session.id)
        .order_by(CoreSessionArtifact.ordinal.desc()).limit(settings.candidate_limit)))
    candidates = []
    # Batch indices are assigned by the immutable task order within each Plan.
    for artifact, task, batch_index in reversed(rows):
        batch = str(task.plan_id)
        node = objects.get(artifact.canvas_node_id, {})
        node = node if isinstance(node, dict) else {}
        chosen = str(artifact.stored_asset_id)
        visible = str(node.get("assetId") or "") == chosen
        if not visible and node.get("assetId") and artifact.core_asset_id:
            try:
                viewed_id = UUID(str(node["assetId"]))
            except ValueError:
                viewed_id = None
            viewed = await tx.scalar(select(CoreAssetVersion.id).where(
                CoreAssetVersion.asset_id == artifact.core_asset_id,
                CoreAssetVersion.stored_asset_id == viewed_id)) if viewed_id else None
            if viewed:
                visible, chosen = True, str(node["assetId"])
        if query["kind"] == "spatial" and not visible:
            continue
        available = True
        try:
            await readable_asset(tx, session, chosen)
        except ValueError:
            available = False
        candidates.append(ArtifactCandidate(artifact_id=str(artifact.id), session_id=str(session.id),
            ordinal=artifact.ordinal, batch_id=batch, batch_index=batch_index,
            asset_id=chosen, permitted=True, available=available,
            selected=explicit and chosen in selected,
            x=node.get("x") if visible and type(node.get("x")) in (int, float) and math.isfinite(node["x"]) else None,
            y=node.get("y") if visible and type(node.get("y")) in (int, float) and math.isfinite(node["y"]) else None))
    if query["kind"] == "spatial":
        indexed_nodes = {artifact.canvas_node_id for artifact, _task, _index in rows}
        extra_nodes = [(key, value) for key, value in objects.items()
            if key not in indexed_nodes and isinstance(value, dict) and value.get("assetId")]
        if len(candidates) + len(extra_nodes) > settings.candidate_limit:
            return copied, {"status": "ambiguous", "reason": "candidate_limit", "candidate_ids": []}
        for node_id, node in extra_nodes:
            try:
                asset = await readable_asset(tx, session, str(node["assetId"]))
            except ValueError:
                continue
            candidates.append(ArtifactCandidate(artifact_id="canvas:" + node_id, session_id=str(session.id),
                ordinal=len(candidates) + 1, asset_id=str(asset.id), permitted=True, available=True,
                x=node.get("x") if type(node.get("x")) in (int, float) and math.isfinite(node["x"]) else None,
                y=node.get("y") if type(node.get("y")) in (int, float) and math.isfinite(node["y"]) else None))
        if any(item.available and item.x is None for item in candidates):
            return copied, {"status": "ambiguous", "reason": "missing_geometry", "candidate_ids": []}
    result = resolve(candidates, ReferenceQuery.model_validate(query), settings)
    if result.status == "resolved":
        await readable_asset(tx, session, result.asset_id)
        copied["selected_assets"] = [result.asset_id]
        indexed = {str(artifact.id): artifact.canvas_node_id for artifact, _task, _index in rows}
        anchor = result.artifact_id.removeprefix("canvas:") if str(result.artifact_id).startswith("canvas:") else indexed.get(result.artifact_id)
        copied["anchor_id"] = anchor or next((key for key, node in objects.items()
            if isinstance(node, dict) and str(node.get("assetId") or "") == result.asset_id), None)
        copied["node_ids"] = [copied["anchor_id"]] if copied["anchor_id"] else []
    return copied, result.model_dump(mode="json")


async def selected_manifest(tx, session, request: dict) -> list[dict]:
    assets = []
    for ident in dict.fromkeys(request.get("selected_assets") or []):
        asset = await readable_asset(tx, session, str(ident))
        assets.append({"asset_id": str(asset.id), "owner_id": session.user_id, "kind": asset.kind,
            "width": asset.width or None, "height": asset.height or None})
    return assets


async def confirmed_decisions(tx, session_id) -> list[dict]:
    rows = list(await tx.scalars(select(CoreSessionDecision).where(
        CoreSessionDecision.session_id == session_id, CoreSessionDecision.superseded_by_id.is_(None))
        .order_by(CoreSessionDecision.created_at, CoreSessionDecision.id)))
    return [{"id": str(row.id), "value": row.value, "source_message_id": str(row.source_message_id)
             if row.source_message_id else None, "confirm_task_id": str(row.confirm_task_id)
             if row.confirm_task_id else None} for row in rows]


async def freeze_selection(tx, run, session, request):
    """Only explicit user selection or code-resolved Session references extend read scope."""
    request, reference = await reference_context(tx, session, request)
    if reference and reference["status"] != "resolved":
        request["reference_clarification"] = reference
    manifest = await selected_manifest(tx, session, request)
    base = {str(value) for value in run.scope.get("readable_assets") or []}
    extra = {item["asset_id"] for item in manifest} - base
    derived = set()
    if extra:
        derived = {str(value) for value in await tx.scalars(select(CoreAssetVersion.stored_asset_id).where(
            CoreAssetVersion.run_id == run.id, CoreAssetVersion.workspace_id == run.workspace_id,
            CoreAssetVersion.category == "artifact", CoreAssetVersion.status == "normal",
            CoreAssetVersion.stored_asset_id.in_([UUID(value) for value in extra])))}
    if extra - derived:
        request["reference_clarification"] = {"status": "unavailable", "reason": "outside_run_scope"}
        request["selected_assets"] = []
        manifest, derived = [], set()
    for item in manifest:
        ident = UUID(item["asset_id"])
        old = await tx.scalar(select(AssetReference.id).where(AssetReference.asset_id == ident,
            AssetReference.owner_type == "core_run", AssetReference.owner_id == str(run.id)))
        if old is None:
            tx.add(AssetReference(asset_id=ident, owner_type="core_run", owner_id=str(run.id)))
    doc = await tx.get(Document, session.canvas_id) if session.canvas_id else None
    node_ids = request.get("node_ids") or []
    if doc and request.get("selection_explicit") and node_ids:
        nodes = (doc.data or {}).get("objects") or {}
        if any(not isinstance(nodes.get(ident), dict) or
               str(nodes[ident].get("assetId") or "") not in {row["asset_id"] for row in manifest} for ident in node_ids):
            request["reference_clarification"] = {"status": "unavailable", "reason": "selection_changed"}
            manifest = []
        elif len(node_ids) == 1:
            request["anchor_id"] = node_ids[0]
    selection = {"selected_assets": request.get("selected_assets") or [], "assets": manifest,
        "derived_asset_ids": sorted(derived),
        "canvas": copy.deepcopy(doc.data) if doc and doc.workspace_id == session.workspace_id else {},
        "document_revision": doc.revision if doc else None, "reference": reference}
    return request, selection, await confirmed_decisions(tx, session.id)


async def effective_scope(tx, run, revision_number=None):
    revision = await tx.scalar(select(CoreRunInputRevision).where(CoreRunInputRevision.run_id == run.id,
        CoreRunInputRevision.revision == (revision_number or run.input_revision)))
    derived = (revision.selection_snapshot or {}).get("derived_asset_ids") or [] if revision else []
    if not derived:
        return dict(run.scope)
    return {**run.scope, "readable_assets": list(dict.fromkeys([*(run.scope.get("readable_assets") or []), *derived]))}


async def effective_run_view(tx, run):
    """Compose the immutable Run with one append-only user revision; never mutate its snapshot."""
    revision = await tx.scalar(select(CoreRunInputRevision).where(
        CoreRunInputRevision.run_id == run.id, CoreRunInputRevision.revision == run.input_revision))
    if revision is None:
        return run
    selection = revision.selection_snapshot or {}
    assets = {row["asset_id"]: row for row in run.snapshot.get("assets") or []}
    assets.update({row["asset_id"]: row for row in selection.get("assets") or []})
    values = {column.key: getattr(run, column.key) for column in CoreRun.__table__.columns}
    values["scope"] = await effective_scope(tx, run)
    values["snapshot"] = {**copy.deepcopy(run.snapshot), **copy.deepcopy(selection.get("canvas") or {}),
        "request": copy.deepcopy(revision.effective_request), "assets": list(assets.values())}
    if selection.get("document_revision") is not None:
        values["snapshot"]["document_revision"] = selection["document_revision"]
    return SimpleNamespace(**values)
