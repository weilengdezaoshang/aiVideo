"""Checkpointed worker. Network I/O occurs after commit, never under database locks."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import logging
import random
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import suppress

from cryptography.fernet import Fernet
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from backend.agent.media.credits import reserve_core, settle_core
from backend.agent.orm import HostCreditLedger
from backend.agent.media.catalog import HostError
from backend.core.asset_cards import CardRejected, apply_card_parameters
from backend.core.contracts import AssetContent
from backend.core.asset_edit import EditRejected, append_image_edit, capability_of
from backend.core.session_context import effective_scope
from backend.core.events.store import alert, emit
from backend.core.orm import CoreAsset, CoreEvent, CoreRun, CoreSession, CoreTask, CoreTaskAttempt, CoreTaskDependency
from backend.core.sessions.fencing import ObsoleteRevision, assert_submittable, latch_first_submit
from backend.core.store import create_asset, transition
from backend.core.tools.catalog import canonical, estimate_cost
from backend.core.tools.contracts import VersionLock
from backend.core.tools.store import load_catalog
from backend.infrastructure.artifacts import ArtifactSpool
from backend.infrastructure.orm import Asset, AssetReference, Document, Job, Outbox
from backend.providers.base import Generated
from backend.services.media import inspect_media
from .contracts import Provider, ProviderResult, WorkerSettings, before_call, after_result
from .layout import place, place_text_leaf
from .orm import CanvasWrite, ExecutionAdmission, ProviderReceipt, TaskExecution


def now():
    return datetime.now(timezone.utc)


# Tools whose provider result is structured JSON instead of artifact bytes.
# detect_subject keeps its dedicated validation; image_analyze returns the
# analyzer answer; card tasks apply their write in the consume transaction.
_STRUCTURED_TOOLS = frozenset({"detect_subject", "image_analyze", "create_asset_card", "update_asset_card", "text_layout"})
_PRICED_STRUCTURED = frozenset({"image_analyze", "text_layout"})
# Read-only tools that still execute through the provider path (network model call).
_PROVIDER_READ_TOOLS = frozenset({"detect_subject", "image_analyze"})


def fingerprint(task):
    return hashlib.sha256(canonical({"id": str(task.id), "plan": str(task.plan_id), "version": task.plan_version,
        "tool": task.tool_ref, "tool_version": task.tool_version, "parameters": task.parameters,
        "scope": task.scope, "invariants": task.invariants}).encode()).hexdigest()


async def record_admission(tx, task, catalog, *, checks, review_ref, placement):
    """Internal reviewer adapter, never an HTTP/model tool. Engine cannot mint approval."""
    if checks != {"legal": True, "types": True, "intent": True} or any(type(value) is not bool for value in checks.values()) or not review_ref or task.status != "validated":
        raise ValueError("all three checks and validated task required")
    run = await tx.get(CoreRun, task.run_id)
    catalog.check_lock(VersionLock.model_validate(run.version_lock["tool_catalog"]))
    if task.plan_id != run.current_plan_id:
        raise ValueError("stale plan")
    row = ExecutionAdmission(task_id=task.id, plan_id=task.plan_id, review_ref=review_ref,
        task_hash=fingerprint(task), catalog_hash=catalog.lock.content_hash, checks=checks, placement=placement)
    task.review_ref = review_ref
    tx.add(row)
    await tx.flush()
    return row


def scoped_canvas_raw(raw: dict, allowed_asset_ids: set[str], readable_nodes: set[str] | None = None) -> dict:
    """Project only explicitly authorized assets and document text nodes."""
    import re
    objects = raw.get("objects") if isinstance(raw, dict) and isinstance(raw.get("objects"), dict) else {}
    kept = {}
    for ident, value in objects.items():
        if not isinstance(ident, str) or not isinstance(value, dict):
            continue
        asset_id = value.get("assetId") if isinstance(value.get("assetId"), str) else None
        if asset_id is None and isinstance(value.get("src"), str):
            match = re.fullmatch(r"/api/assets/([0-9a-fA-F-]{36})/content", value["src"])
            asset_id = match[1] if match else None
        if asset_id is None:
            if str(value.get("kind") or value.get("type") or "").lower() != "text" or ident not in (readable_nodes or set()):
                continue
            if any(not isinstance(value.get(key), (int, float)) or isinstance(value.get(key), bool) for key in ("x", "y", "width", "height")):
                continue
            copied = {key: value[key] for key in ("x", "y", "width", "height", "rotation", "name", "text", "fontSize", "fontFamily", "fill") if key in value}
            copied["kind"] = "text"
            from backend.agent.tools.canvas_read import CanvasElement
            from pydantic import ValidationError
            try:
                CanvasElement.model_validate({**copied, "id": ident})
            except ValidationError:
                # A malformed legacy note must not invalidate unrelated authorized reads.
                continue
        else:
            if asset_id not in allowed_asset_ids:
                continue
            copied = {key: value[key] for key in ("kind", "x", "y", "width", "height", "rotation", "name", "ext") if key in value}
            copied["assetId"] = asset_id
        kept[ident] = copied
    order = raw.get("order") if isinstance(raw, dict) else None
    return {"objects": kept, "order": [item for item in order if isinstance(item, str) and item in kept] if isinstance(order, list) else []}


_UNREADABLE_ASSET = frozenset({"deleted", "purged", "reported", "taken_down"})


def _asset_blocked(asset, workspace_id) -> bool:
    """Workspace, host block, and reported/taken-down states are not readable."""
    if asset is None or asset.workspace_id != workspace_id:
        return True
    details = asset.details or {}
    if details.get("hostBlocked"):
        return True
    return details.get("status") in _UNREADABLE_ASSET


def _read_gateway(tool=None):
    """Read tools only. Model write tools are not registered on this gateway.

    Spec timeout comes from the frozen tool SLA. A bare gateway uses the catalog
    local timeout, never a fixed 5 second stand-in.
    """
    from pydantic import BaseModel, ConfigDict, Field

    from backend.agent.contracts import Coverage
    from backend.agent.tools.canvas_read import READ_TOOLS
    from backend.agent.tools.errors import ToolError
    from backend.agent.tools.registry import Outcome, ToolGateway, ToolSpec
    from backend.core.tools.contracts import ToolSettings

    sla = tool.execution.sla_seconds if tool is not None else ToolSettings().local_timeout_seconds

    class AssetQuery(BaseModel):
        model_config = ConfigDict(extra="forbid", populate_by_alias=True)
        asset_id: str = Field(alias="asset_id", min_length=1, max_length=128)

    def read_asset(parsed, context, keep):
        binding = context.assets.get(parsed.asset_id)
        if not isinstance(binding, dict) or not binding.get("readable"):
            raise ToolError("ASSET_OUT_OF_SCOPE", "素材不在本次运行可读范围内")
        return Outcome(data={"asset_id": parsed.asset_id, "kind": binding.get("kind"),
            "width": binding.get("width"), "height": binding.get("height")},
            coverage=Coverage(complete=True), asset_ids=(parsed.asset_id,))

    specs = []
    for item in READ_TOOLS:
        timeout = sla if tool is not None and item.name == tool.name else item.timeout_s
        if timeout == item.timeout_s:
            specs.append(item)
        else:
            specs.append(ToolSpec(item.name, item.version, item.description, item.input, item.kind,
                item.stages, timeout, item.handler, item.facts, item.suggest))
    asset_timeout = sla if tool is None or tool.name == "read_asset" else ToolSettings().local_timeout_seconds
    specs.append(ToolSpec("read_asset", 1, "读取授权范围内资产的基本元数据。", AssetQuery, "read",
        frozenset({"execute"}), asset_timeout, read_asset, lambda outcome, parsed: [], lambda outcome, parsed: []))
    return ToolGateway(specs)


_BAD_DEPENDENCY = frozenset({"failed", "cancelled", "blocked_by_model", "rejected"})


async def _dependency_gate(tx, task, run):
    identifiers = list(await tx.scalars(select(CoreTaskDependency.depends_on_id).where(CoreTaskDependency.task_id == task.id)))
    for ident in identifiers:
        upstream = await tx.get(CoreTask, ident)
        if upstream is None or upstream.run_id != run.id:
            return "invalid"
        if upstream.status == "succeeded":
            continue
        if upstream.status in _BAD_DEPENDENCY:
            return "invalid"
        return "pending"
    return "ready"


async def _canvas_assets(tx, run, doc, media_root: Path, scope=None):
    """Current workspace, frozen readable set, live reference, and a path under media_root."""
    from backend.agent.media.inspection import managed_path
    readable = {str(item) for item in (scope if scope is not None else run.scope or {}).get("readable_assets") or []}
    if doc is None or doc.workspace_id != run.workspace_id or not readable:
        return {}
    found = {}
    for ident in readable:
        try:
            asset_id = uuid.UUID(ident)
        except ValueError:
            continue
        asset = await tx.get(Asset, asset_id)
        if _asset_blocked(asset, run.workspace_id):
            continue
        referenced = await tx.scalar(select(AssetReference.id).where(AssetReference.asset_id == asset.id,
            AssetReference.owner_type == "document", AssetReference.owner_id == str(doc.id)))
        if referenced is None:
            continue
        binding = {"kind": asset.kind, "width": asset.width, "height": asset.height, "ext": asset.ext,
            "sha256": asset.sha256, "bytes": asset.bytes, "readable": True}
        try:
            managed_path(media_root, asset)
        except HostError:
            binding["file_ready"] = False
        else:
            binding["storage_key"] = asset.storage_key
            binding["file_ready"] = True
        found[str(asset.id)] = binding
    return found


async def enqueue(tx, task_id, settings=None):
    """Join the EXISTING Job/outbox/Celery phase queue, in the caller transaction."""
    settings = settings or WorkerSettings()
    task = await tx.get(CoreTask, task_id, with_for_update=True)
    if not task or task.status != "queued" or task.kind != "call_tool":
        raise ValueError("only queued call_tool tasks can be enqueued")
    if task.job_id:
        return task.job_id
    run = await tx.get(CoreRun, task.run_id)
    admission = await tx.scalar(select(ExecutionAdmission).where(ExecutionAdmission.task_id == task.id))
    if not admission or admission.task_hash != fingerprint(task) or admission.plan_id != task.plan_id or admission.checks != {"legal": True, "types": True, "intent": True}:
        raise ValueError("execution admission required")
    catalog = await load_catalog(tx, run.version_lock["tool_catalog"]["version"],
        expected_lock=VersionLock.model_validate(run.version_lock["tool_catalog"]))
    tool = catalog.get_tool(task.tool_ref, catalog.lock.version)
    if tool.name not in run.scope.get("tools", []) and f"{tool.name}@{tool.version}" not in run.scope.get("tools", []):
        raise ValueError("tool outside Run scope")
    read_only = tool.mode == "read_only" and tool.name not in _PROVIDER_READ_TOOLS
    output = next((p.media_type for p in tool.outputs if p.media_type in {"image", "video", "audio"}), None)
    if not read_only and output is None and tool.name not in _STRUCTURED_TOOLS:
        raise ValueError("this executor requires an artifact-producing tool")
    kind = "read" if read_only else "export" if tool.execution.adapter in {"frames_to_video", "export.timeline"} else "video" if output == "video" else "image"
    job = Job(workspace_id=run.workspace_id, document_id=(await tx.get(CoreSession, run.session_id)).canvas_id,
        kind=kind, params={"coreTaskId": str(task.id)},
        provider_snapshot={"provider": "core_worker"}, phase="core_worker", status="queued")
    tx.add(job)
    await tx.flush()
    task.job_id = job.id
    cause = await tx.scalar(select(CoreEvent.id).where(CoreEvent.aggregate_id == task.id).order_by(CoreEvent.aggregate_version.desc()).limit(1))
    from backend.providers.policy import ProviderPolicy
    tx.add(TaskExecution(task_id=task.id, job_id=job.id, causation_id=cause, deadline=now() + timedelta(seconds=ProviderPolicy().queue_s)))
    tx.add(Outbox(aggregate_type="job", aggregate_id=str(job.id), event_type="execution.due", payload={"jobId": str(job.id)}))
    await tx.flush()
    return job.id


class Worker:
    def __init__(self, factory, root: Path, provider: Provider | None, *, settings=None, before=None, after=None):
        self.factory, self.root, self.provider = factory, root, provider
        self.settings = settings or WorkerSettings()
        self.before, self.after = before or before_call, after or after_result
        if not self.settings.ticket_key:
            raise ValueError("explicit AIVERO_EXECUTION_TICKET_KEY required")
        self.cipher = Fernet(self.settings.ticket_key.encode())
        self.spool = ArtifactSpool(root)

    def seal(self, value):
        return self.cipher.encrypt(canonical(value).encode()).decode()

    def open(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    async def rows(self, tx, job_id, *, canvas=False):
        await tx.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": f"{self.settings.lock_timeout_ms}ms"})
        execution = await tx.scalar(select(TaskExecution).where(TaskExecution.job_id == job_id))
        if execution is None:
            return None
        task = await tx.get(CoreTask, execution.task_id)
        if task is None:
            raise ValueError("execution task missing")
        session = await tx.get(CoreSession, (await tx.get(CoreRun, task.run_id)).session_id)
        doc = None
        if session.canvas_id:
            if canvas and not execution.cancel_requested:
                doc = await tx.scalar(select(Document).where(Document.id == session.canvas_id).with_for_update(skip_locked=True))
                if doc is None:
                    return None
            else:
                doc = await tx.get(Document, session.canvas_id)
        run = await tx.scalar(select(CoreRun).where(CoreRun.id == task.run_id).with_for_update(skip_locked=True))
        if run is None:
            return None
        task = await tx.scalar(select(CoreTask).where(CoreTask.id == task.id).with_for_update(skip_locked=True).execution_options(populate_existing=True))
        execution = await tx.scalar(select(TaskExecution).where(TaskExecution.job_id == job_id).with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if task is None or execution is None:
            return None
        return execution, task, run, doc, await tx.get(Job, job_id)

    async def references(self, tx, task, run, tool):
        scope = await effective_scope(tx, run, task.input_revision)
        deps = set(await tx.scalars(select(CoreTaskDependency.depends_on_id).where(CoreTaskDependency.task_id == task.id)))
        by_key = {}
        for ident in deps:
            upstream = await tx.get(CoreTask, ident)
            if upstream is None or upstream.run_id != run.id or upstream.status != "succeeded":
                raise HostError("dependencies_pending", "依赖尚未成功")
            if upstream.plan_id != task.plan_id:
                raise HostError("dependencies_pending", "依赖不属于当前计划")
            by_key[upstream.task_key] = upstream
        async def resolve(value, allow_reference=True):
            if allow_reference and isinstance(value, str) and value.startswith("$"):
                import re
                match = re.fullmatch(r"\$([a-z][a-z0-9_]*)\.(output|candidates\[\d+\]\.mask)", value)
                if not match or match[1] not in by_key:
                    raise ValueError("invalid dependency reference")
                upstream_task = by_key[match[1]]
                output = ((upstream_task.waiting or {}).get("selected_output") if upstream_task.tool_ref == "detect_subject" and match[2] == "output" else upstream_task.output)
                if output is None:
                    raise HostError("dependencies_pending", "等待主体选择")
                if match[2] != "output":
                    from backend.agent.admission.tool_types import _read
                    try:
                        output = _read(output, match[2])
                    except (KeyError, IndexError, TypeError, ValueError):
                        raise ValueError("approved output reference no longer exists") from None
                resolved_output = await resolve(output, False)
                if upstream_task.tool_ref == "detect_subject" and match[2] == "output":
                    resolved_output = {**resolved_output, "_members": upstream_task.waiting["selected_handles"]}
                return resolved_output
            if isinstance(value, list):
                return [await resolve(item, allow_reference) for item in value]
            if isinstance(value, dict):
                result = {k: await resolve(v, allow_reference) for k, v in value.items()}
                if result.get("media_type") == "card" and "card_id" in result:
                    from backend.core.asset_cards import resolve_card, run_created_card_ids
                    frozen = set((run.version_lock or {}).get("asset_cards") or [])
                    created = (str(result["card_id"]) not in frozen
                               and str(result["card_id"]) in await run_created_card_ids(tx, run))
                    view = await resolve_card(tx, run, result["card_id"], allow_run_created=created)
                    return {**result, **view, "resolved": True}
                if "asset_id" in result:
                    asset = await tx.get(Asset, uuid.UUID(result["asset_id"]))
                    allowed = set(scope.get("readable_assets", []))
                    generated = {str((t.output or {}).get("asset_id")) for t in by_key.values()}
                    if asset is None or asset.workspace_id != run.workspace_id or asset.details.get("hostBlocked") or str(asset.id) not in allowed | generated:
                        raise ValueError("asset unavailable or out of scope")
                    if result.get("media_type", asset.kind) != asset.kind:
                        raise ValueError("asset type mismatch")
                    result.update(storage_key=asset.storage_key, ext=asset.ext, sha256=asset.sha256,
                        duration_frames=round(asset.details.get("durationSec", 0) * 30))
                    if asset.kind == "text":
                        from backend.core.composition.persist import materialize_text_reference
                        from backend.core.orm import CoreAssetVersion
                        path = (Path(self.root) / asset.storage_key).resolve()
                        if not path.is_relative_to(Path(self.root).resolve()) or not path.is_file():
                            raise ValueError("text source missing")
                        version = await tx.scalar(select(CoreAssetVersion).where(
                            CoreAssetVersion.workspace_id == run.workspace_id,
                            CoreAssetVersion.stored_asset_id == asset.id,
                        ).order_by(CoreAssetVersion.version.desc()).limit(1))
                        result = materialize_text_reference(result, storage_key=asset.storage_key,
                            sha256=asset.sha256, asset_id=str(asset.id), blob=path.read_bytes(),
                            core_asset_id=None if version is None else str(version.asset_id),
                            core_asset_version_id=None if version is None else str(version.id))
                return result
            return value
        parameters = copy.deepcopy(task.parameters)
        async def hydrate(value, parts):
            if not parts:
                return await resolve(value)
            head, *tail = parts
            if not isinstance(value, dict):
                return value
            if head.endswith("[*]"):
                key = head[:-3]
                if isinstance(value.get(key), list):
                    value[key] = [await hydrate(item, tail) for item in value[key]]
            elif head in value:
                value[head] = await hydrate(value[head], tail)
            return value
        for port in tool.inputs:
            parameters = await hydrate(parameters, port.path.split("."))
        return parameters

    async def execute(self, job_id):
        token = uuid.uuid4()
        async with self.factory.begin() as tx:
            rows = await self.rows(tx, job_id)
            if rows is None:
                return {"deferred": "busy_or_missing"}
            execution, task, run, doc, job = rows
            if execution.phase == "done":
                return {"done": True}
            if execution.lease_until and execution.lease_until > now():
                return {"deferred": "leased"}
            if execution.next_at > now():
                return {"deferred": "not_due"}
            admission = await tx.scalar(select(ExecutionAdmission).where(ExecutionAdmission.task_id == task.id))
            if not admission or admission.task_hash != fingerprint(task) or admission.review_ref != task.review_ref or admission.plan_id != task.plan_id or admission.checks != {"legal": True, "types": True, "intent": True}:
                raise ValueError("admission missing or changed")
            catalog = await load_catalog(tx, run.version_lock["tool_catalog"]["version"], expected_lock=VersionLock.model_validate(run.version_lock["tool_catalog"]))
            tool = catalog.get_tool(task.tool_ref, catalog.lock.version)
            if tool.name not in run.scope.get("tools", []) and f"{tool.name}@{tool.version}" not in run.scope.get("tools", []):
                raise ValueError("tool outside Run scope")
            if tool.rollout_groups and run.scope.get("rollout_group") not in tool.rollout_groups:
                raise ValueError("tool outside frozen rollout scope")
            if admission.catalog_hash != catalog.lock.content_hash or tool.version != task.tool_version:
                raise ValueError("frozen binding mismatch")
            read_only = tool.mode == "read_only" and tool.name not in _PROVIDER_READ_TOOLS
            if read_only:
                if task.status in {"succeeded", "failed", "cancelled", "blocked_by_model"} or execution.phase == "done":
                    return {"done": True}
                if run.status != "running" or task.status != "queued" or task.plan_id != run.current_plan_id:
                    return {"deferred": "run_or_task_not_ready"}
                if now() > execution.deadline:
                    await self.finish(tx, rows, "failed", 0, "queue_timeout")
                    return {"failed": "queue_timeout"}
                return await self._execute_read(tx, rows, tool)
            if self.provider is None:
                raise RuntimeError("HTTP receipt ingress cannot execute provider operations")
            if execution.phase == "ready":
                if run.status != "running" or task.status != "queued" or task.plan_id != run.current_plan_id:
                    return {"deferred": "run_or_task_not_ready"}
                if now() > execution.deadline:
                    await self.finish(tx, rows, "failed", 0, "queue_timeout")
                    return {"failed": "queue_timeout"}
                execution.deadline = now() + timedelta(seconds=tool.execution.sla_seconds)
                try:
                    params = await self.references(tx, task, run, tool)
                except HostError:
                    await self.defer(tx, execution, job)
                    return {"deferred": "dependencies"}
                except ValueError:
                    await self.finish(tx, rows, "failed", 0, "input_unavailable")
                    return {"failed": "input_unavailable"}
                try:
                    params = apply_card_parameters(tool, params)
                except ValueError:
                    await self.finish(tx, rows, "failed", 0, "param_invalid")
                    return {"failed": "param_invalid"}
                from backend.agent.admission.tool_types import _values
                for port in tool.inputs:
                    for value in _values(params, port.path.split(".")):
                        if not isinstance(value, dict) or value.get("media_type") != port.media_type:
                            await self.finish(tx, rows, "failed", 0, "input_type_mismatch")
                            return {"failed": "input_type_mismatch"}
                rewrite = await self.before(task)
                if rewrite is not None and rewrite != params:
                    raise ValueError("hook_rewrite_requires_fresh_admission")
                if tool.execution.adapter == "staged" and task.parameters.get("count", 1) != 1:
                    raise ValueError("expand and audit batch tasks before execution")
                try:
                    await assert_submittable(tx, run, task)
                except ObsoleteRevision:
                    await self.finish(tx, rows, "cancelled", 0, "obsolete_input_revision")
                    return {"cancelled": "obsolete_input_revision"}
                quote = estimate_cost(tool, task.parameters)
                try:
                    await reserve_core(tx, run, task, quote)
                except HostError as exc:
                    await emit(tx, "budget_exceeded", run.id, causation_id=execution.causation_id, ids={"task_id": task.id})
                    await self.defer(tx, execution, job)
                    return {"deferred": exc.code}
                execution.resolved_parameters = params
                try:
                    await assert_submittable(tx, run, task)
                except ObsoleteRevision:
                    await self.finish(tx, rows, "cancelled", 0, "obsolete_input_revision")
                    return {"cancelled": "obsolete_input_revision"}
                await transition(tx, "task", task.id, task.version, "submitted", causation_id=execution.causation_id)
                await latch_first_submit(tx, run, task)
                execution.phase = "preparing" if hasattr(self.provider, "prepare") else "sending"
            if run.status == "cancelled":
                execution.cancel_requested = True
            if execution.cancel_requested and execution.phase in {"preparing", "sending"} and (execution.attempt_id is None or (await tx.get(CoreTaskAttempt, execution.attempt_id)).submission_status == "registered"):
                await self.finish(tx, rows, "cancelled", 0)
                return {"cancelled": True}
            if execution.phase in {"preparing", "sending"} and execution.attempt_id is None:
                attempt = CoreTaskAttempt(task_id=task.id, run_id=run.id, attempt=task.retry_count + 1,
                    idempotency_key=hashlib.sha256(f"{run.id}:{task.id}:{task.idempotency_key}:{task.retry_count}".encode()).hexdigest(),
                    provider_ref=hashlib.sha256(canonical(tool.execution.binding).encode()).hexdigest(),
                    submission_status="registered", submission_certainty="not_sent", registration={
                        "parameter_hash": hashlib.sha256(canonical(execution.resolved_parameters).encode()).hexdigest()})
                tx.add(attempt)
                await tx.flush()
                execution.attempt_id = attempt.id
            attempt = await tx.get(CoreTaskAttempt, execution.attempt_id)
            phase = execution.phase
            # Sending is durable BEFORE network I/O. A lost lease never permits resubmission.
            first_send = phase == "sending" and attempt.submission_status == "registered"
            if first_send:
                attempt.submission_status, attempt.submission_certainty = "sending", "unknown"
            if execution.phase in {"unknown", "poll"} and now() > execution.deadline:
                await alert(tx, "submission_unknown", None, f"execution:{task.id}", context={"task_id": str(task.id), "run_id": str(run.id)})
                execution.next_at = job.next_poll_at = now() + timedelta(seconds=self.settings.reconcile_seconds)
                if execution.phase != "unknown":
                    await emit(tx, "timeout", run.id, causation_id=execution.causation_id, ids={"task_id": task.id})
                task.error = {"code": "provider_timeout"}
                execution.phase = "unknown"
                return {"unknown": True}
            task.heartbeat_at = run.heartbeat_at = now()
            job.status = "running"
            execution.lease_token, execution.lease_until = token, now() + timedelta(seconds=self.settings.lease_seconds)
            previous_epoch = execution.artifact_epoch
            execution.version += 1
            write_epoch = execution.version
            execution.artifact_epoch = write_epoch
            parameters = {"_execution_epoch": write_epoch, **execution.resolved_parameters, **({"_prepared": execution.prepared} if hasattr(self.provider, "prepare") else {})}
            attempt_id, provider_task_id, idem = attempt.id, attempt.provider_task_id, attempt.idempotency_key
            cancelled = execution.cancel_requested or run.status == "cancelled"
            ticket = self.open(execution.ticket) if execution.ticket else None
            artifact = execution.artifact
            can_cancel = tool.execution.cancellable and self.provider.cancellable
            can_idempotent = (tool.execution.provider_idempotent or tool.mode == "deterministic") and self.provider.idempotent
            kind = next((p.media_type for p in tool.outputs if p.media_type in {"image", "video", "audio"}), "mask")
        heartbeat = asyncio.create_task(self.heartbeat(job_id, token))
        try:
            measured = None
            if phase == "publish":
                self.spool.verify(artifact)
                measured = inspect_media((self.root / artifact["storage_key"]).read_bytes(), kind, artifact["ext"])
                result = None
            elif phase == "preparing":
                prepared = await asyncio.wait_for(self.provider.prepare(parameters), self.settings.call_timeout_seconds)
                result = None
            elif phase == "download":
                # Artifact receipt may have survived a kill between fsync and the DB checkpoint.
                artifact = self.spool.read(job_id, previous_epoch)
                if artifact is None:
                    data, ext = await asyncio.wait_for(self.provider.download(ticket, kind), self.settings.call_timeout_seconds)
                    inspect_media(data, kind, ext)
                    artifact = self.spool.save(job_id, write_epoch, Generated(data, ext))
                result = None
            elif ticket and "provider_reply" in ticket and phase in {"sending", "unknown", "poll"}:
                result = ProviderResult(**ticket["provider_reply"])
            elif cancelled and provider_task_id and can_cancel:
                result = await asyncio.wait_for(self.provider.cancel(provider_task_id), self.settings.call_timeout_seconds)
            elif first_send:
                result = await asyncio.wait_for(self.provider.submit(str(attempt_id), parameters,
                    idem if can_idempotent else None), self.settings.call_timeout_seconds)
            else:
                result = await asyncio.wait_for(self.provider.query(provider_task_id, str(attempt_id)), self.settings.call_timeout_seconds)
                if result is None and can_idempotent:
                    result = await asyncio.wait_for(self.provider.submit(str(attempt_id), parameters, idem), self.settings.call_timeout_seconds)
            if result and result.data:
                inspect_media(result.data, kind, result.ext)
                artifact = self.spool.save(job_id, write_epoch, Generated(result.data, result.ext))
            if result and not await self.checkpoint_reply(job_id, token, result, artifact):
                return {"deferred": "fenced"}
            async with self.factory.begin() as tx:
                rows = await self.rows(tx, job_id, canvas=phase == "publish")
                if rows is None:
                    await self.release_owner(job_id, token)
                    return {"deferred": "publish_busy"}
                execution, task, run, doc, job = rows
                if execution.lease_token != token:
                    return {"deferred": "fenced"}
                if phase == "publish":
                    await self.publish(tx, rows, admission.placement, measured)
                elif phase == "preparing":
                    execution.prepared, execution.phase = prepared, "sending"
                elif result:
                    await self.consume(tx, rows, result)
                    if artifact and execution.phase == "download":
                        execution.artifact, execution.phase = artifact, "publish"
                elif artifact:
                    execution.artifact, execution.phase = artifact, "publish"
                else:
                    execution.phase = "unknown"
                    execution.retries += 1
                    await alert(tx, "submission_unknown", None, f"execution:{task.id}", context={"task_id": str(task.id), "run_id": str(run.id)})
                await self.defer(tx, execution, job, immediate=execution.phase in {"download", "publish"})
            if execution.phase in {"download", "publish", "sending"}:
                return await self.execute(job_id)
            return {"phase": execution.phase}
        except (TimeoutError, OSError, ValueError, KeyError) as exc:
            async with self.factory.begin() as tx:
                rows = await self.rows(tx, job_id)
                if rows is None:
                    return {"deferred": "error_busy"}
                execution, task, run, doc, job = rows
                if execution.lease_token != token:
                    return {"deferred": "fenced"}
                if phase == "sending":
                    execution.phase = "unknown"
                    task.error = {"code": "submission_unknown"}
                    await emit(tx, "timeout", run.id, causation_id=execution.causation_id, ids={"task_id": task.id})
                elif phase == "download" and execution.expires_at and now() >= execution.expires_at:
                    await self.finish(tx, rows, "failed", 0, "artifact_expired")
                elif phase == "download":
                    execution.retries += 1
                elif phase == "preparing":
                    execution.retries += 1
                    if isinstance(exc, ValueError) or execution.retries > task.max_retries:
                        await self.finish(tx, rows, "failed", 0, "prepare_failed")
                elif phase == "publish":
                    if isinstance(exc, EditRejected):
                        execution.retries += 1
                        if exc.code != "asset_version_conflict" or execution.retries > task.max_retries:
                            await self.finish(tx, rows, "failed", 0, exc.code)
                        # The artifact is already on MediaHost. A version CAS retry
                        # never downloads again or re-submits the provider request.
                    elif execution.expires_at and now() < execution.expires_at:
                        execution.phase, execution.artifact = "download", None
                    else:
                        await self.finish(tx, rows, "failed", 0, "artifact_unavailable")
                else:
                    execution.retries += 1
                    if execution.retries > task.max_retries:
                        execution.phase, execution.deadline = "unknown", now()
                    await alert(tx, "execution_error", None, f"execution-error:{task.id}", context={"task_id": str(task.id), "phase": phase, "error_type": type(exc).__name__})
                await self.defer(tx, execution, job)
            return {"deferred": "recoverable_or_unknown"}
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def heartbeat(self, job_id, token):
        while True:
            await asyncio.sleep(min(self.settings.heartbeat_seconds, self.settings.lease_seconds / 3))
            try:
                async with self.factory.begin() as tx:
                    await tx.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": f"{self.settings.lock_timeout_ms}ms"})
                    changed = await tx.execute(update(TaskExecution).where(TaskExecution.job_id == job_id,
                        TaskExecution.lease_token == token).values(lease_until=now() + timedelta(seconds=self.settings.lease_seconds)))
                    if not changed.rowcount:
                        return
            except Exception:
                return  # DB loss cannot license a second submit; recovery queries the durable registration.

    async def checkpoint_reply(self, job_id, token, result, artifact):
        async with self.factory.begin() as tx:
            await tx.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": f"{self.settings.lock_timeout_ms}ms"})
            record = await tx.scalar(select(TaskExecution).where(TaskExecution.job_id == job_id).with_for_update(skip_locked=True))
            if record is None or record.lease_token != token or record.phase == "done":
                return False
            attempt = await tx.get(CoreTaskAttempt, record.attempt_id)
            if attempt.provider_task_id and attempt.provider_task_id != result.task_id:
                raise ValueError("provider task identity mismatch")
            attempt.provider_task_id = result.task_id
            attempt.submission_certainty = "accepted" if result.submission_accepted else "rejected"
            data = asdict(result)
            data.pop("data", None)
            record.ticket = self.seal({"provider_reply": data})
            if artifact:
                record.artifact = artifact
            return True

    async def release_owner(self, job_id, token):
        async with self.factory.begin() as tx:
            record = await tx.scalar(select(TaskExecution).where(TaskExecution.job_id == job_id).with_for_update(skip_locked=True))
            if record is not None and record.lease_token == token:
                record.lease_until, record.lease_token = now(), None
                job = await tx.get(Job, job_id)
                job.dispatched_until = None
                job.next_poll_at = now()

    async def defer(self, tx, execution, job, *, immediate=False):
        if execution.phase == "done":
            return
        delay = 0 if immediate else min(self.settings.retry_max_seconds, self.settings.retry_base_seconds * 2 ** min(execution.retries, max(0, math.ceil(math.log2(self.settings.retry_max_seconds / self.settings.retry_base_seconds)))))
        if execution.phase == "poll":
            delay = self.settings.poll_seconds
        delay += random.uniform(0, delay * self.settings.retry_jitter)
        if execution.phase == "download" and execution.expires_at:
            delay = min(delay, max(0, (execution.expires_at - now()).total_seconds()))
        execution.next_at = now() + timedelta(seconds=delay)
        execution.lease_token = execution.lease_until = None
        job.next_poll_at, job.dispatched_until = execution.next_at, None
        job.status = "running"
        job.state_version += 1
        tx.add(Outbox(aggregate_type="job", aggregate_id=str(job.id), event_type="execution.due",
            next_attempt_at=execution.next_at, payload={"jobId": str(job.id)}))

    async def consume(self, tx, rows, result):
        execution, task, run, doc, job = rows
        attempt = await tx.get(CoreTaskAttempt, execution.attempt_id)
        if attempt.provider_task_id and attempt.provider_task_id != result.task_id:
            raise ValueError("provider task identity mismatch")
        attempt.provider_task_id, attempt.submission_status = result.task_id, "submitted"
        attempt.submission_certainty = "accepted" if result.submission_accepted else "rejected"
        job.external_task_id = result.task_id
        if result.status == "pending":
            execution.phase, execution.ticket = "poll", None
        elif result.status == "cancelled":
            await self.finish(tx, rows, "cancelled", 0)
        elif result.status == "failed":
            if result.failure in {None, "unknown"}:
                await alert(tx, "unknown_tool_failure", None, f"provider-failure:{attempt.id}", context={"task_id": str(task.id), "run_id": str(run.id)})
            if result.failure == "retryable" and task.retry_count < task.max_retries and not execution.cancel_requested:
                attempt.submission_status = "finished"
                task.retry_count += 1
                execution.retries += 1
                execution.phase, execution.attempt_id, execution.ticket = "sending", None, None
            else:
                await self.finish(tx, rows, "blocked_by_model" if result.failure == "blocked_by_model" else "failed", 0, result.failure)
        else:
            # Callbacks and polls converge through this unique provider-scoped receipt.
            data = asdict(result)
            data.pop("data", None)
            provider_ref = attempt.provider_ref
            result_key = result.task_id or "sync:" + str(attempt.id)
            await tx.execute(insert(ProviderReceipt).values(provider_ref=provider_ref, provider_task_id=result_key,
                task_id=task.id, result_hash=hashlib.sha256(canonical(data).encode()).hexdigest(), ticket=self.seal(data))
                .on_conflict_do_nothing(index_elements=[ProviderReceipt.provider_ref, ProviderReceipt.provider_task_id]))
            receipt = await tx.scalar(select(ProviderReceipt).where(ProviderReceipt.provider_ref == provider_ref,
                ProviderReceipt.provider_task_id == result_key))
            if receipt.task_id != task.id or receipt.result_hash != hashlib.sha256(canonical(data).encode()).hexdigest():
                raise ValueError("provider receipt conflict")
            if result.actual_credits > task.credits_reserved:
                execution.phase = "unknown"
                execution.deadline = now()
                await alert(tx, "unapproved_cost", None, f"cost:{task.id}", context={"task_id": str(task.id), "actual": result.actual_credits, "reserved": task.credits_reserved})
                return
            if task.tool_ref == "compose_layers" and isinstance(result.locator, dict) and result.locator.get("composition"):
                execution.prepared = {**(execution.prepared or {}), "composition": result.locator["composition"]}
            if result.output is not None and result.locator is None:
                if task.tool_ref not in _STRUCTURED_TOOLS or (result.actual_credits != 0 and task.tool_ref not in _PRICED_STRUCTURED):
                    raise ValueError("unexpected structured result")
                from backend.core.tools.detection import DetectionResult
                from jsonschema import Draft202012Validator
                result_catalog = await load_catalog(tx, run.version_lock["tool_catalog"]["version"],
                    expected_lock=VersionLock.model_validate(run.version_lock["tool_catalog"]))
                definition = result_catalog.get_tool(task.tool_ref, result_catalog.lock.version)
                output = result.output
                invalid_code = "recognition_output_invalid"
                if task.tool_ref in {"create_asset_card", "update_asset_card"}:
                    from backend.core.asset_cards import apply_card_task
                    try:
                        output = await apply_card_task(tx, task, execution, run)
                    except CardRejected as exc:
                        await self.finish(tx, rows, "failed", 0, exc.code)
                        return
                    invalid_code = "card_output_invalid"
                if task.tool_ref == "image_analyze":
                    invalid_code = "analyze_output_invalid"
                if task.tool_ref == "text_layout":
                    from backend.core.composition.persist import persist_text_layout
                    try:
                        output = await persist_text_layout(tx, Path(self.root), attempt.id, run, task, output,
                                                           job_id=job.id)
                    except ValueError:
                        await self.finish(tx, rows, "failed", 0, "param_invalid")
                        return
                    await self._retain_text_canvas(tx, rows, output)
                    invalid_code = "param_invalid"
                    if result.actual_credits != definition.price.credits:
                        await self.finish(tx, rows, "failed", 0, "param_invalid")
                        return
                if not Draft202012Validator(definition.result_schema).is_valid(output):
                    await self.finish(tx, rows, "failed", 0, invalid_code)
                    return
                if task.tool_ref == "detect_subject":
                    try:
                        output = DetectionResult.model_validate(output).model_dump(mode="json")
                    except ValueError:
                        await self.finish(tx, rows, "failed", 0, "recognition_output_invalid")
                        return
                task.output = output
                await self.finish(tx, rows, "succeeded", result.actual_credits if task.tool_ref in _PRICED_STRUCTURED else 0)
                return
            execution.result_credits = result.actual_credits
            execution.ticket = self.seal(result.locator or {})
            execution.expires_at = datetime.fromtimestamp(result.expires_at, timezone.utc) if result.expires_at else now() + timedelta(seconds=self.settings.reconcile_seconds)
            execution.phase = "download"

    async def _execute_read(self, tx, rows, tool):
        """Same worker entry as generation. No credit ledger and no canvas write."""
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError, ValidationError
        from backend.agent.tools.canvas_read import project_snapshot
        execution, task, run, doc, job = rows
        gate = await _dependency_gate(tx, task, run)
        if gate == "pending":
            await self.defer(tx, execution, job)
            return {"deferred": "dependencies"}
        if gate == "invalid":
            await self.finish(tx, rows, "failed", 0, "param_invalid")
            return {"failed": "param_invalid"}
        try:
            Draft202012Validator(tool.parameters).validate(task.parameters or {})
        except (SchemaError, ValidationError):
            await self.finish(tx, rows, "failed", 0, "param_invalid")
            return {"failed": "param_invalid"}
        try:
            resolved = await self.references(tx, task, run, tool)
            params = {key: resolved[key] for key in (task.parameters or {})} if isinstance(resolved, dict) else task.parameters
        except HostError:
            await self.defer(tx, execution, job)
            return {"deferred": "dependencies"}
        except ValueError:
            await self.finish(tx, rows, "failed", 0, "param_invalid")
            return {"failed": "param_invalid"}
        if doc is not None and doc.workspace_id != run.workspace_id:
            doc = None
        media_root = Path(self.root).resolve()
        scope = await effective_scope(tx, run, task.input_revision)
        assets = await _canvas_assets(tx, run, doc, media_root, scope)
        if tool.name == "read_asset":
            asset_id = str((params or {}).get("asset_id") or "")
            own = await self._readable_workspace_asset(tx, run, asset_id, scope)
            assets = {asset_id: own} if own else {}
        raw = scoped_canvas_raw(doc.data if doc is not None else {}, set(assets),
            {str(item) for item in (run.scope or {}).get("readable_nodes") or []})
        selected = [item for item in ((run.snapshot or {}).get("selectedObjectIds") or []) if item in raw["objects"]]
        snapshot = {"id": str(doc.id) if doc is not None else "", "name": doc.name if doc is not None else "",
            "revision": doc.revision if doc is not None else 0, **raw}
        try:
            snapshot = project_snapshot(snapshot, selected)
        except Exception:
            await self.finish(tx, rows, "failed", 0, "param_invalid")
            return {"failed": "param_invalid"}
        before = None if doc is None else json.dumps(doc.data, sort_keys=True, default=str)
        ordinal = await self._read_ordinal(tx, task)
        inspect_assets = {key: value for key, value in assets.items() if value.get("file_ready") and "storage_key" in value}
        bound = assets if tool.name == "read_asset" else inspect_assets if tool.name == "inspect_image" else assets
        outcome = _read_gateway(tool).execute(tool.name, params or {}, snapshot, bound, media_root,
            max_output_bytes=self.settings.read_max_bytes, ordinal=ordinal, call_id=str(task.id))
        if doc is not None and json.dumps(doc.data, sort_keys=True, default=str) != before:
            await self.finish(tx, rows, "failed", 0, "param_invalid")
            return {"failed": "param_invalid"}
        if not isinstance(outcome, dict) or outcome.get("error") or "result" not in outcome:
            await self.finish(tx, rows, "failed", 0, "param_invalid")
            return {"failed": "param_invalid"}
        output = outcome
        if len(canonical(output).encode()) > self.settings.read_max_bytes or not Draft202012Validator(tool.result_schema).is_valid(output):
            await self.finish(tx, rows, "failed", 0, "param_invalid")
            return {"failed": "param_invalid"}
        task.output = output
        try:
            await assert_submittable(tx, run, task)
        except ObsoleteRevision:
            await self.finish(tx, rows, "cancelled", 0, "obsolete_input_revision")
            return {"cancelled": "obsolete_input_revision"}
        await transition(tx, "task", task.id, task.version, "submitted", causation_id=execution.causation_id)
        await latch_first_submit(tx, run, task)
        await self.finish(tx, rows, "succeeded", 0)
        return {"done": True}

    async def _readable_workspace_asset(self, tx, run, asset_id, scope=None):
        readable = {str(item) for item in (scope if scope is not None else run.scope or {}).get("readable_assets") or []}
        if not asset_id or asset_id not in readable:
            return None
        try:
            ident = uuid.UUID(asset_id)
        except ValueError:
            return None
        asset = await tx.get(Asset, ident)
        if _asset_blocked(asset, run.workspace_id):
            return None
        return {"asset_id": str(asset.id), "kind": asset.kind, "width": asset.width, "height": asset.height, "readable": True}

    async def _read_ordinal(self, tx, task):
        earlier = await tx.scalar(select(func.count()).select_from(CoreTask).where(CoreTask.run_id == task.run_id,
            CoreTask.kind == "call_tool", or_(CoreTask.created_at < task.created_at,
                and_(CoreTask.created_at == task.created_at, CoreTask.id < task.id))))
        return int(earlier or 0) + 1

    async def finish(self, tx, rows, status, actual, reason=None):
        execution, task, run, doc, job = rows
        if execution.phase == "done":
            return
        ledger = await tx.scalar(select(HostCreditLedger).where(HostCreditLedger.core_task_id == task.id))
        if ledger is not None:
            await settle_core(tx, run, task, actual, cancelled=execution.cancel_requested)
        elif actual or (task.status != "queued" and task.credits_reserved != 0):
            raise ValueError("missing reservation for submitted task")
        task.error = {"code": reason} if reason else None
        if execution.attempt_id:
            attempt = await tx.get(CoreTaskAttempt, execution.attempt_id)
            attempt.actual_credits, attempt.submission_status, attempt.finished_at = actual, "finished", now()
            attempt.error = {"code": reason} if reason else None
        result = await transition(tx, "task", task.id, task.version, status, causation_id=execution.causation_id) if task.status != status else None
        from backend.core.batch.runtime import settle_marked_dispatch
        await settle_marked_dispatch(tx, run, task)
        event = "task_succeeded" if status == "succeeded" else "task_blocked" if status == "blocked_by_model" else "task_failed" if status == "failed" else "user_cancelled"
        await emit(tx, event, run.id, causation_id=result.event_id if result else execution.causation_id, ids={"task_id": task.id})
        execution.phase, execution.ticket = "done", None
        for receipt in await tx.scalars(select(ProviderReceipt).where(ProviderReceipt.task_id == task.id)):
            receipt.ticket = None
        execution.lease_token = execution.lease_until = None
        job.error_code = reason
        job.status = "completed" if status == "succeeded" else "failed"
        job.phase, job.finished_at = "completed" if status == "succeeded" else "failed", now()
        job.next_poll_at = job.dispatched_until = None
        job.state_version += 1
        job.progress = 1.0
        tx.add(Outbox(aggregate_type="job", aggregate_id=str(job.id), event_type="job.changed",
            payload={"jobId": str(job.id), "workspaceId": str(run.workspace_id)}))

    async def _retain_text_canvas(self, tx, rows, layout):
        """A leaf text task may draw. suppress_canvas keeps the asset and writes history only."""
        execution, task, run, doc, _job = rows
        if doc is None or await tx.get(CanvasWrite, task.id):
            return
        from .layout import canvas_insert_allowed
        admission = await tx.scalar(select(ExecutionAdmission).where(ExecutionAdmission.task_id == task.id))
        placement = (admission.placement if admission else {}) or {}
        suppress = placement.get("suppress_canvas") is True
        cancelled = execution.cancel_requested or run.status == "cancelled" or task.status == "cancelled"
        node, conflict, rect = None, False, {}
        if (not suppress and canvas_insert_allowed(cancel_requested=execution.cancel_requested,
                run_cancelled=run.status == "cancelled", task_cancelled=task.status == "cancelled",
                permitted=run.scope.get("write_canvas") is True)):
            node, conflict, rect = await place_text_leaf(doc, task.id, placement, self.settings, layout)
            if node:
                tx.add(AssetReference(asset_id=uuid.UUID(layout["asset_id"]), owner_type="document",
                                      owner_id=str(doc.id)))
        tx.add(CanvasWrite(task_id=task.id, document_id=doc.id, node_id=node,
            asset_id=uuid.UUID(layout["asset_id"]), conflict=bool(conflict),
            history_only=suppress or cancelled or node is None, rect=rect or {}))

    async def publish(self, tx, rows, placement, measured):
        execution, task, run, doc, job = rows
        if await tx.get(CanvasWrite, task.id):
            return
        artifact = execution.artifact
        asset = Asset(workspace_id=run.workspace_id, kind="video" if job.kind in {"video", "export"} else "image",
            ext=artifact["ext"], storage_key=artifact["storage_key"], bytes=artifact["bytes"],
            sha256=artifact["sha256"], width=measured["width"], height=measured["height"], details={"history": True, "coreTaskId": str(task.id), "coreRunId": str(run.id),
                "durationSec": task.parameters.get("duration_seconds", 0), "params": {"kind": "video" if job.kind in {"video", "export"} else "image",
                    "prompt": task.parameters.get("prompt", ""), "width": measured["width"], "height": measured["height"]}})
        tx.add(asset)
        await tx.flush()
        manifest = (execution.prepared or {}).get("composition") if task.tool_ref == "compose_layers" else None
        if manifest:
            from backend.core.orm import CoreAssetVersion
            for layer in manifest.get("layers") or []:
                if layer.get("source_version_id") or not layer.get("asset_id"):
                    continue
                found = await tx.scalar(select(CoreAssetVersion).where(
                    CoreAssetVersion.workspace_id == run.workspace_id,
                    CoreAssetVersion.stored_asset_id == uuid.UUID(str(layer["asset_id"])),
                ).order_by(CoreAssetVersion.version.desc()).limit(1))
                if found is not None:
                    layer["source_version_id"] = str(found.id)
        content = AssetContent(
            kind=asset.kind, category="artifact", storage={"asset_id": str(asset.id)}, stored_asset_id=asset.id,
            content_hash=asset.sha256, user_id=run.user_id, run_id=run.id, task_id=task.id,
            width=asset.width, height=asset.height, size_bytes=asset.bytes, layers=manifest or {})
        edit_capability = None if task.tool_ref == "cutout" else capability_of(task.tool_ref or "")
        if edit_capability:
            source_id = (execution.resolved_parameters.get("image") or {}).get("asset_id")
            source = await tx.get(Asset, uuid.UUID(str(source_id))) if source_id else None
            if source is None or source.workspace_id != run.workspace_id:
                raise EditRejected("edit_source_missing")
            edited = await append_image_edit(tx, tool_name=task.tool_ref, resolved_asset_id=source.id,
                workspace_id=run.workspace_id, user_id=run.user_id, run_id=run.id, task_id=task.id,
                result=content, original=AssetContent(kind=source.kind, category="artifact",
                    storage={"asset_id": str(source.id)}, stored_asset_id=source.id,
                    content_hash=source.sha256, user_id=run.user_id, width=source.width,
                    height=source.height, size_bytes=source.bytes))
            core_asset = await tx.get(CoreAsset, edited.core_asset_id)
            version_id = edited.version_id
        else:
            core_asset = await create_asset(tx, run.workspace_id, run.user_id, "personal", content)
            version_id = core_asset.current_version_id
        if manifest:
            from backend.core.orm import CoreAssetLineage
            for layer in manifest.get("layers") or []:
                source_id = layer.get("source_version_id")
                if source_id:
                    tx.add(CoreAssetLineage(input_version_id=uuid.UUID(str(source_id)), output_version_id=version_id, relation="composed_from"))
        cancelled = execution.cancel_requested or run.status == "cancelled" or task.status == "cancelled"
        suppress_canvas = placement.get("suppress_canvas") is True
        node, conflict, rect = None, False, {}
        if doc and not cancelled and not suppress_canvas:
            if doc.workspace_id != run.workspace_id:
                raise ValueError("document workspace mismatch")
            safe_placement = {**placement, "writable_region": run.scope.get("writable_region")}
            permitted = run.scope.get("write_canvas") is True
            if "writable_regions" in run.scope:
                region_id = placement.get("region_id")
                bounds = run.snapshot.get("regions", {}).get(region_id) or run.snapshot.get("objects", {}).get(region_id)
                permitted = region_id in run.scope["writable_regions"] and isinstance(bounds, dict) and all(key in bounds for key in ("x", "y", "width", "height"))
                if permitted:
                    safe_placement["writable_region"] = {key: bounds[key] for key in ("x", "y", "width", "height")}
            from .layout import canvas_insert_allowed
            if canvas_insert_allowed(cancel_requested=execution.cancel_requested, run_cancelled=run.status == "cancelled",
                                     task_cancelled=task.status == "cancelled", permitted=permitted):
                node, conflict, rect = await place(doc, task.id, asset, safe_placement, self.settings, manifest)
            else:
                conflict = True
            if node:
                tx.add(AssetReference(asset_id=asset.id, owner_type="document", owner_id=str(doc.id)))
            if conflict:
                await emit(tx, "conflict", run.id, causation_id=execution.causation_id, ids={"task_id": task.id, "asset_id": core_asset.id})
        if doc:
            tx.add(CanvasWrite(task_id=task.id, document_id=doc.id, node_id=node, asset_id=asset.id,
                conflict=conflict, history_only=cancelled or suppress_canvas or node is None, rect=rect))
        tx.add(AssetReference(asset_id=asset.id, owner_type="job", owner_id=str(job.id)))
        task.output = {"asset_id": str(asset.id), "media_type": asset.kind, "core_asset_id": str(core_asset.id),
            "core_asset_version_id": str(version_id), **({"layers": manifest} if manifest else {})}
        execution.asset_id = asset.id
        job.phase_payload = {**artifact, "assetId": str(asset.id)}
        extra_events = await self.after(task, asset, tx)
        for event in extra_events or []:
            await emit(tx, event["type"], run.id, causation_id=execution.causation_id, ids=event["ids"])
        await self.finish(tx, rows, "cancelled" if cancelled else "succeeded", execution.result_credits)

    async def callback(self, job_id, provider_ref, result):
        """Caller authenticated the provider. Never trusts user-provided run/task bindings."""
        async with self.factory.begin() as tx:
            completed = await tx.scalar(select(TaskExecution.task_id).where(TaskExecution.job_id == job_id, TaskExecution.phase == "done"))
            if completed is not None:
                logging.getLogger(__name__).debug("completed worker request acknowledged task_id=%s", completed)
                return True
            rows = await self.rows(tx, job_id)
            if rows is None:
                return False
            execution, task, run, doc, job = rows
            if execution.phase == "done":
                return True
            attempt = await tx.get(CoreTaskAttempt, execution.attempt_id) if execution.attempt_id else None
            if attempt is None or attempt.provider_ref != provider_ref:
                raise ValueError("callback not bound to this provider attempt")
            if attempt.provider_task_id is None:
                # An early callback cannot bind an arbitrary task. Recovery queries registration first.
                return False
            if attempt.provider_task_id != result.task_id:
                return True  # callback for a previous attempt
            if execution.phase in {"download", "publish"}:
                return True
            await self.consume(tx, rows, result)
            await self.defer(tx, execution, job, immediate=execution.phase in {"download", "publish"})
            return True

    async def cancel(self, job_id):
        async with self.factory.begin() as tx:
            completed = await tx.scalar(select(TaskExecution.task_id).where(TaskExecution.job_id == job_id, TaskExecution.phase == "done"))
            if completed is not None:
                logging.getLogger(__name__).debug("completed worker request acknowledged task_id=%s", completed)
                return True
            rows = await self.rows(tx, job_id)
            if rows is None:
                return False
            execution, task, run, doc, job = rows
            if execution.phase == "done":
                return True
            execution.cancel_requested = True
            if execution.phase == "ready":
                changed = await transition(tx, "task", task.id, task.version, "cancelled")
                await emit(tx, "user_cancelled", run.id, causation_id=changed.event_id, ids={"task_id": task.id})
                execution.phase, job.status, job.phase = "done", "failed", "failed"
                job.finished_at = now()
                job.state_version += 1
                tx.add(Outbox(aggregate_type="job", aggregate_id=str(job.id), event_type="job.changed", payload={"jobId": str(job.id)}))
                return True
            # Do not invalidate an in-flight submit. Let it finish, then cancel/query its handle.
            execution.next_at = job.next_poll_at = now()
            return True


async def scan_ready(factory, settings=None):
    """One invalid/locked task must never stall unrelated existing generation jobs."""
    from sqlalchemy.exc import DBAPIError
    settings = settings or WorkerSettings()
    async with factory() as tx:
        identifiers = list(await tx.scalars(select(CoreTask.id).join(ExecutionAdmission, ExecutionAdmission.task_id == CoreTask.id)
            .where(CoreTask.status == "queued", CoreTask.job_id.is_(None)).order_by(CoreTask.created_at).limit(settings.scan_limit)))
    count = 0
    for ident in identifiers:
        try:
            async with factory.begin() as tx:
                await tx.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": f"{settings.lock_timeout_ms}ms"})
                task = await tx.scalar(select(CoreTask).where(CoreTask.id == ident).with_for_update(skip_locked=True))
                if task and task.job_id is None:
                    await enqueue(tx, task.id, settings)
                    count += 1
        except ValueError:
            async with factory.begin() as tx:
                await alert(tx, "execution_admission_invalid", None, f"admission:{ident}", context={"task_id": str(ident)})
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != "55P03":
                raise
    return count
