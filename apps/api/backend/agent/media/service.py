"""Durable media Host. Each tick reconciles receipts and atomically dispatches a ready layer.

No provider network calls occur here. The existing phase workers own submission,
reconciliation, downloads and cancellation. Document -> Session -> Run is the lock order.
"""
from __future__ import annotations

import copy
from datetime import timedelta
import hashlib
import re
import os
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import func, select, text

from backend.agent.kernel import Kernel
from backend.agent.orm import AgentRun, AgentSession, AgentStep, HostCreditLedger, HostNodeLease
from backend.agent.util import encoded, error, now
from backend.infrastructure.orm import Asset, AssetReference, Document, Job
from backend.services.acceptance import accept_generation
from . import canvas, credits
from backend.agent.admission.policy import IntentContract, Proposal, digest
from backend.agent.admission.service import propose, require_admission
from .catalog import Catalog, HostError
from .canvas_actions import is_cutout_request, prepare_cutout, validate_cutout_result
from .request_context import resolve_context
from .contracts import HostFinal, HostTurn, HostRetry, MediaPlan
from .planner import plan_intent, plan_from_decision, plan_from_multi, validate_plan
from .intent import IntentPipeline, IntentSettings
from .intent.types import ClarifyTicket
from .intent.sequential import detect_sequence
from .intent.negation import affirmative_text
from .intent.feedback_log import candidate, reference_snapshot

MEDIA_ACTIVE = ("queued", "running", "canceling", "needs_attention")
CHILD_TERMINAL = {"completed", "failed", "cancelled", "blocked", "skipped"}


class MediaHost(Kernel):
    def __init__(self, factory, catalog: Catalog | None = None, media_root: Path | None = None):
        super().__init__(factory)
        self.catalog = catalog or Catalog.load(None)
        self.media_root = media_root
        self.intent = IntentPipeline(self.catalog, IntentSettings.from_env())
        self.environment = self.catalog.config.environment if self.catalog.config.enabled else os.environ.get("AIVERO_RUNTIME_ENVIRONMENT", "production")

    async def get(self, workspace, principal, ident):
        async with self.factory.begin() as db:
            _, _, run = await self._locked(db, ident, workspace)
            if run.principal != principal:
                raise HTTPException(404, "创作运行不存在")
            return {"run": await self._project(db, run)}

    async def snapshot(self, workspace, principal, document, request_id=None):
        async with self.factory.begin() as db:
            doc = await db.get(Document, document)
            if doc is None or doc.workspace_id != workspace:
                raise HTTPException(404, "文档不存在")
            session = await db.scalar(select(AgentSession).where(AgentSession.workspace_id == workspace,
                AgentSession.source_document_id == document, AgentSession.principal == principal))
            if session is None:
                return {"sessionId": None, "documentId": str(document), "cursor": 0, "runs": [], "requestedRun": None}
            runs = list(await db.scalars(select(AgentRun).where(AgentRun.session_id == session.id)
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(50)))
            requested = await db.scalar(select(AgentRun).where(AgentRun.session_id == session.id,
                AgentRun.client_request_id == request_id)) if request_id else None
            return {"sessionId": str(session.id), "documentId": str(document), "cursor": session.cursor,
                "runs": [await self._project(db, run) for run in reversed(runs)],
                "requestedRun": await self._project(db, requested) if requested else None}

    async def events(self, workspace, principal, document, after=0):
        from backend.agent.orm import AgentEvent
        async with self.factory.begin() as db:
            doc = await db.get(Document, document)
            if doc is None or doc.workspace_id != workspace:
                raise HTTPException(404, "文档不存在")
            session = await db.scalar(select(AgentSession).where(AgentSession.workspace_id == workspace,
                AgentSession.source_document_id == document, AgentSession.principal == principal))
            if session is None:
                return {"events": [], "cursor": 0}
            rows = list(await db.scalars(select(AgentEvent).where(AgentEvent.session_id == session.id,
                AgentEvent.seq > after).order_by(AgentEvent.seq).limit(100)))
            return {"events": [row.payload for row in rows], "cursor": rows[-1].seq if rows else min(after, session.cursor)}

    async def _locked(self, db, ident, workspace=None):
        row = await db.get(AgentRun, ident)
        if row is None or row.phase != "media" or (workspace is not None and row.workspace_id != workspace):
            raise HTTPException(404, "创作运行不存在")
        doc = await db.get(Document, uuid.UUID(row.snapshot["id"]), with_for_update=True)
        session, run = await self._lock_run(db, ident, workspace)
        return doc, session, run

    async def _steps(self, db, run):
        return list(await db.scalars(select(AgentStep).where(AgentStep.run_id == run.id,
                                    AgentStep.plan_version == run.plan["version"])
                                   .order_by(AgentStep.ordinal).with_for_update()))

    async def _sources(self, db, workspace, ids):
        try:
            keys = [uuid.UUID(ident) for ident in ids]
        except (ValueError, TypeError):
            raise HostError("asset_forbidden", "参考资产标识无效", status=400) from None
        rows = list(await db.scalars(select(Asset).where(Asset.id.in_(keys)).order_by(Asset.id).with_for_update())) if keys else []
        if len(rows) != len(set(keys)) or any(asset.workspace_id != workspace or asset.details.get("hostBlocked") for asset in rows):
            raise HostError("asset_forbidden", "参考资产不可用或无权限", status=404)
        return {str(asset.id): asset for asset in rows}

    async def _admit_text(self, db, doc, session, principal, request, fingerprint, reply, *, sources=None, clarify=False, canvas_action=None, decision=None, failure=None, feedback=None):
        if session is None:
            session = AgentSession(workspace_id=doc.workspace_id, document_id=doc.id,
                source_document_id=doc.id, principal=principal, cursor=0)
            db.add(session)
            await db.flush()
        run = AgentRun(session_id=session.id, workspace_id=doc.workspace_id, principal=principal,
            client_request_id=request.clientRequestId, request_hash=fingerprint,
            status="failed" if failure else "waiting_approval" if canvas_action else "waiting_input" if clarify else "completed", phase="media", stage="waiting" if canvas_action else "route" if clarify else "done",
            error=error(failure.code, failure.message) if failure else None,
            message=request.message, reply=reply, route={"id": "host"}, snapshot={"id": str(doc.id),
            "revision": doc.revision, "selectedObjectIds": request.selectedObjectIds}, assets={}, history=[], counters={},
            waiting={"question": reply, "stage": "intent",
                **({"options": [{"id": item.id, "label": item.label} for item in decision.clarify.options]}
                    if decision and decision.clarify else {})} if clarify else None,
            execution={"plan": None, "tier": "preview", "inputAssetIds": list(sources or {}), "quoteCredits": 0,
                **({"canvasAction": canvas_action} if canvas_action else {}),
                **({"intentTrace": decision.trace, "intentDecision": decision.model_dump(mode="json")} if decision else {}),
                "intentFeedback": [feedback] if feedback else []})
        db.add(run)
        await db.flush()
        for asset in (sources or {}).values():
            db.add(AssetReference(asset_id=asset.id, owner_type="agent_run", owner_id=str(run.id)))
        return {"run": await self._event(db, session, run, "host.clarifying" if clarify else "host.text"), "replayed": False}

    async def _supersede_before_plan(self, workspace, principal, request, fingerprint):
        # A failed replacement plan must not roll back the user's explicit cancellation.
        async with self.factory.begin() as db:
            doc, session, old = await self._locked(db, uuid.UUID(request.supersedeRunId), workspace)
            if doc is None or str(doc.id) != request.documentId or old.principal != principal:
                raise HostError("asset_forbidden", "不能替代其他用户或画布的任务", status=403)
            previous = await db.scalar(select(AgentRun).where(AgentRun.session_id == session.id,
                AgentRun.client_request_id == request.clientRequestId))
            if previous:
                if previous.request_hash != fingerprint:
                    raise HostError("idempotency_conflict", "同一请求标识不能更改目标")
                return {"run": await self._project(db, previous), "replayed": True}
            await self._cancel_locked(db, session, old, "user_revised")
        return None

    async def create(self, workspace, principal, raw):
        request = HostTurn.model_validate(raw)
        document_id = uuid.UUID(request.documentId)
        if request.context and (request.context.doc_id != request.documentId or
                request.context.request_id != request.clientRequestId or
                request.context.doc_version != request.baseRevision or
                request.context.selection.ids != request.selectedObjectIds or
                request.context.request_text().strip() != request.message):
            raise HostError("CONTEXT_MISMATCH", "请求上下文与本次消息不一致", status=400)
        fingerprint = hashlib.sha256(encoded(request.model_dump(mode="json")).encode()).hexdigest()
        status_question = bool(re.fullmatch(r"(?:现在|当前|这批|任务)?[，, ]*(?:进度(?:怎么样|如何|到哪了)?|状态(?:怎么样)?|到哪了|多久|完成了吗|怎么样了|还要多久|生成好了吗)[？?。 ]*", request.message))
        cancel_request = bool(re.fullmatch(r"(?:取消|停止|不要了|结束|停下来|cancel|stop)(?:这一批|当前任务|生成)?[。！! ]*", request.message, re.I))
        acknowledgement = bool(re.fullmatch(r"你好|谢谢|好的|了解|辛苦了|hi|hello", request.message, re.I))
        if request.supersedeRunId and not status_question and not acknowledgement:
            replay = await self._supersede_before_plan(workspace, principal, request, fingerprint)
            if replay:
                return replay
        async with self.factory.begin() as db:
            doc = await db.get(Document, document_id, with_for_update=True)
            if doc is None or doc.workspace_id != workspace:
                raise HTTPException(404, "文档不存在")
            session = await db.scalar(select(AgentSession).where(AgentSession.workspace_id == workspace,
                AgentSession.source_document_id == document_id, AgentSession.principal == principal).with_for_update())
            if session:
                previous = await db.scalar(select(AgentRun).where(AgentRun.session_id == session.id,
                    AgentRun.client_request_id == request.clientRequestId))
                if previous:
                    if previous.request_hash != fingerprint or previous.principal != principal:
                        raise HostError("idempotency_conflict", "同一请求标识不能更改目标或用户")
                    return {"run": await self._project(db, previous), "replayed": True}
                if session.closed_at:
                    raise HostError("node_missing", "画布会话已关闭")
            if doc.revision != request.baseRevision:
                raise HostError("STALE_VERSION" if request.context else "node_version_conflict", "画布版本已变化，请保存后重新发送")
            objects = doc.data.get("objects", {})
            if any(node not in objects for node in request.selectedObjectIds):
                raise HostError("node_missing", "选择的节点已不存在")
            if status_question or cancel_request or acknowledgement:
                current = await db.scalar(select(AgentRun).where(AgentRun.session_id == session.id,
                    AgentRun.phase == "media", AgentRun.status.in_(MEDIA_ACTIVE)).order_by(AgentRun.created_at.desc()).limit(1)) if session else None
                if cancel_request:
                    if current:
                        await self._cancel_locked(db, session, current, "user_cancel")
                    reply = "已请求取消当前任务，成功作品保留。" if current else "当前没有进行中的创作任务。"
                elif acknowledgement:
                    reply = "可以继续描述创作目标，作品会放到画布上。"
                elif current:
                    projection = await self._project(db, current)
                    media = projection.get("media") or {}
                    reply = f"当前已完成 {media.get('completed', 0)}/{media.get('total', 0)} 个步骤。"
                else:
                    reply = "当前没有进行中的创作任务。"
                return await self._admit_text(db, doc, session, principal, request, fingerprint, reply)
            if self.intent.settings.enabled:
                return await self._create_layered(db, doc, session, principal, request, fingerprint)
            context_snapshot = None
            if request.context:
                if session and request.context.conversation_id != str(session.id):
                    raise HostError("CONTEXT_MISMATCH", "请求会话与当前画布不一致", status=409)
                try:
                    primary, context_snapshot = resolve_context(request.context, doc.data, doc.revision, request.message)
                except HostError as exc:
                    if is_cutout_request(request.message) and exc.code == "REFERENCE_REQUIRED":
                        return await self._admit_text(db, doc, session, principal, request, fingerprint,
                            "请在画布选中一张已导入的图片，再发送抠图要求", clarify=True)
                    raise
                request = request.model_copy(update={"selectedObjectIds": primary})
            ignores_source = bool(re.search(r"不要参考|忽略参考|纯文生", request.message))
            if not ignores_source and any(objects[node].get("kind") in {"image", "video"} and not objects[node].get("assetId") for node in request.selectedObjectIds):
                raise HostError("asset_forbidden", "参考素材须先导入当前工作区资产库", status=422)
            references = list(dict.fromkeys([*request.referenceAssetIds,
                *(objects[node]["assetId"] for node in request.selectedObjectIds if objects[node].get("assetId"))]))
            sources = await self._sources(db, workspace, references)
            if not ignores_source and any(asset.kind != "image" for asset in sources.values()):
                raise HostError("capability_unavailable", "当前创作路径仅支持图片参考", status=422)
            message = request.message
            goal_type = None
            clarification = None
            if request.clarifyRunId:
                _, _, clarification = await self._locked(db, uuid.UUID(request.clarifyRunId), workspace)
                if session is None or clarification.principal != principal or clarification.session_id != session.id or clarification.status != "waiting_input":
                    raise HostError("confirmation_expired", "待澄清任务已失效")
                if not references:
                    references = clarification.execution["inputAssetIds"]
                selection = re.search(r"第([一二三四1234])张", message)
                if selection and references:
                    index = {"一": 0, "二": 1, "三": 2, "四": 3, "1": 0, "2": 1, "3": 2, "4": 3}[selection[1]]
                    if index >= len(references):
                        raise HostError("intent_clarify", "选择的参考图不存在，请重新选择", status=422)
                    references = [references[index]]
                sources = await self._sources(db, workspace, references)
                goal_type = "image" if re.search(r"只要.*(?:图片|图)|不要视频|不需要视频", message) else "video" if re.search(r"视频|动画", message) else None
                pending_sequence = detect_sequence(clarification.message)
                if pending_sequence and message.strip() in {"先做第一步", "先做A"}:
                    message = pending_sequence.parts[0]
                elif pending_sequence and not detect_sequence(message) and message.strip() not in {"一次做完", "都做", "好的"}:
                    # 旧路径也允许用户按提示拆成新的单步，不能把原顺位句再次拼回去。
                    message = request.message
                else:
                    message = f"{clarification.message}；补充需求：{message}"
            try:
                if detect_sequence(message):
                    raise HostError("intent_clarify", "当前请先确认第一步，或把顺位需求拆成两次发送", status=422)
                if is_cutout_request(message):
                    selected = list(request.selectedObjectIds)
                    # A reply such as “选好了” can supply the missing image from the current selection.
                    if not selected and clarification and context_snapshot:
                        selected = context_snapshot["selection_ids"]
                    action = {**prepare_cutout(message, selected, objects), "documentId": str(doc.id)}
                    sources = await self._sources(db, workspace, [action["sourceAssetId"]])
                    if any(asset.kind != "image" for asset in sources.values()):
                        raise HostError("intent_clarify", "请选择可用的图片素材进行抠图", status=422)
                    request = request.model_copy(update={"selectedObjectIds": selected})
                    if clarification:
                        clarification.status, clarification.stage, clarification.waiting = "completed", "done", None
                        await self._event(db, session, clarification, "host.intent_resolved")
                    return await self._admit_text(db, doc, session, principal, request, fingerprint,
                        "请在画布检查主体选区，可用画笔和橡皮擦修正，最后点击「抠图」确认。", sources=sources, canvas_action=action)
                if self.intent.settings.shadow:
                    await self.intent.run(message, references, request_id=request.clientRequestId,
                        snapshot=reference_snapshot(doc, request.selectedObjectIds))
                plan = plan_intent(message, references, self.catalog, goal_type=goal_type)
            except HostError as exc:
                if exc.code != "intent_clarify":
                    raise
                return await self._admit_text(db, doc, session, principal, request, fingerprint, exc.message, sources=sources, clarify=True)
            sources = {ident: asset for ident, asset in sources.items() if ident in plan.sourceAssetIds}
            if clarification:
                clarification.status, clarification.stage, clarification.waiting = "completed", "done", None
                await self._event(db, session, clarification, "host.intent_resolved")
            if session is None:
                session = AgentSession(workspace_id=workspace, document_id=document_id,
                    source_document_id=document_id, principal=principal, cursor=0)
                db.add(session)
                await db.flush()
            return await self._admit(db, doc, session, principal, request.clientRequestId, fingerprint,
                                     plan, sources, selected=request.selectedObjectIds, context_snapshot=context_snapshot, intent_message=message,
                                     intent_confirmed=clarification is not None)

    async def _create_layered(self, db, doc, session, principal, request, fingerprint):
        if request.context is None:
            raise HostError("CONTEXT_REQUIRED", "请刷新画布后重新发送，创作请求必须携带上下文", status=400)
        if session and request.context.conversation_id != str(session.id):
            raise HostError("CONTEXT_MISMATCH", "请求会话与当前画布不一致", status=409)
        clarification, feedback = None, None
        if request.clarifyRunId:
            _, _, clarification = await self._locked(db, uuid.UUID(request.clarifyRunId), doc.workspace_id)
            if (session is None or clarification.principal != principal or clarification.session_id != session.id
                    or clarification.status != "waiting_input" or not clarification.execution.get("intentDecision")):
                raise HostError("confirmation_expired", "待澄清任务已失效", status=409)
            ticket = ClarifyTicket.model_validate(clarification.execution["intentDecision"]["clarify"])
            selected = list(ticket.snapshot["nodes"])
            snapshot = reference_snapshot(doc, selected)
            decision = self.intent.resume(ticket, request.message, snapshot=snapshot,
                option_id=request.clarifyOptionId, trace=clarification.execution.get("intentTrace"))
            references, message = ticket.references, ticket.message
            sources = await self._sources(db, doc.workspace_id, references)
            context_snapshot = snapshot
            if not decision.clarify:
                feedback = candidate("clarify_click" if request.clarifyOptionId else "clarify_text",
                    message, decision.l2, ticket.request_id, ticket.snapshot,
                    user_action=request.clarifyOptionId or request.message,
                    steps=[step.model_dump(mode="json") for step in decision.steps or []])
        else:
            selected, context_snapshot = resolve_context(request.context, doc.data, doc.revision, request.message, layered=True)
            objects = doc.data.get("objects", {})
            references = list(dict.fromkeys([*request.referenceAssetIds,
                *(objects[node]["assetId"] for node in selected if objects[node].get("assetId") and objects[node].get("kind") == "image")]))
            sources = await self._sources(db, doc.workspace_id, references)
            if any(asset.kind != "image" for asset in sources.values()):
                raise HostError("capability_unavailable", "此操作需要图片参考，不能使用视频替代", status=422)
            if re.search(r"不要参考|忽略参考|纯文生", request.message):
                references, sources, selected = [], {}, []
            message = request.message
            decision = await self.intent.run(message, references, request_id=request.clientRequestId,
                snapshot=reference_snapshot(doc, selected), input_from="upload" if references and not selected else "selection")
        if session:
            pending = list(await db.scalars(select(AgentRun).where(AgentRun.session_id == session.id,
                AgentRun.status == "waiting_input", AgentRun.phase == "media")))
            for old in pending:
                if old is not clarification or decision.clarify:
                    old.status, old.waiting = "cancelled", None
                    await self._event(db, session, old, "host.intent_expired")
        request = request.model_copy(update={"selectedObjectIds": selected})
        if decision.clarify:
            return await self._admit_text(db, doc, session, principal, request, fingerprint,
                decision.clarify.question, sources=sources, clarify=True, decision=decision)
        if decision.l2 == "other":
            return await self._admit_text(db, doc, session, principal, request, fingerprint,
                "可以继续描述创作目标，作品会放到画布上。", decision=decision)
        if decision.l2 == "image_edit" and decision.slots.get("op") == "matting" and len(decision.steps or []) <= 1:
            action = {**prepare_cutout(affirmative_text(decision.slots["prompt"]), selected, doc.data.get("objects", {})), "documentId": str(doc.id)}
            result = await self._admit_text(db, doc, session, principal, request, fingerprint,
                "请在画布检查主体选区并确认抠图。", sources=sources, canvas_action=action, decision=decision)
        else:
            try:
                plan = (plan_from_multi(decision, references, self.catalog) if len(decision.steps or []) > 1
                    and self.intent.settings.multi_step else plan_from_decision(decision, references, self.catalog))
            except HostError as exc:
                # 决策已确认但能力不可执行时保存失败与审校候选，不降级成图片。
                result = await self._admit_text(db, doc, session, principal, request, fingerprint,
                    exc.message, decision=decision, failure=exc, feedback=feedback)
                if clarification:
                    clarification.status, clarification.stage, clarification.waiting = "completed", "done", None
                    await self._event(db, session, clarification, "host.intent_resolved")
                return result
            sources = {ident: asset for ident, asset in sources.items() if ident in plan.sourceAssetIds}
            if session is None:
                session = AgentSession(workspace_id=doc.workspace_id, document_id=doc.id,
                    source_document_id=doc.id, principal=principal, cursor=0)
                db.add(session)
                await db.flush()
            result = await self._admit(db, doc, session, principal, request.clientRequestId, fingerprint,
                plan, sources, selected=selected, context_snapshot=context_snapshot, decision=decision, feedback=feedback, intent_message=message,
                intent_confirmed=clarification is not None)
        if clarification:
            clarification.status, clarification.stage, clarification.waiting = "completed", "done", None
            if feedback:
                clarification.execution = {**clarification.execution, "intentFeedback": [feedback]}
            await self._event(db, session, clarification, "host.intent_resolved")
        return result

    async def _admit(self, db, doc, session, principal, request_id, fingerprint, plan, sources,
                     *, selected=(), preview_run=None, preview_refs=None, seeds=None, context_snapshot=None, decision=None, feedback=None, intent_message=None, intent_confirmed=False):
        for item in plan.steps:
            cap = self.catalog.resolve(item.capabilityId, item.tier)
            references = len(item.inputFrom) if item.inputFrom else (1 if (preview_refs or {}).get(item.id) else len(sources))
            if cap.binding.engine == "provider" and references > cap.maxRefImages:
                raise HostError("spec_not_allowed", "参考图数量超出能力目录限制", status=400)
        quote = self.catalog.quote(plan)
        waiting = not self.catalog.config.autoStartPreview or quote > self.catalog.config.confirmCreditsAbove
        snapshots = {step.capabilityId: self.catalog.resolve(step.capabilityId, step.tier).model_dump(mode="json")
                     for step in plan.steps}
        run = AgentRun(session_id=session.id, workspace_id=session.workspace_id, principal=principal,
            client_request_id=request_id, request_hash=fingerprint, status="awaiting_confirm" if waiting else "queued",
            phase="media", stage="execute", message=plan.goal, route={"id": "host"}, assets={}, history=[],
            snapshot={"id": str(doc.id), "revision": doc.revision, "selectedObjectIds": list(selected),
                "context": context_snapshot,
                "selectedSources": {node: {key: doc.data["objects"][node].get(key) for key in ("assetId", "assetVersion")}
                    for node in selected if doc.data["objects"][node].get("assetId") in sources}},
            plan={"goal": plan.goal, "version": 1, "current": None}, counters={},
            execution={"plan": plan.model_dump(mode="json"), "tier": plan.tier, "quoteCredits": quote,
                "catalog": snapshots, "catalogVersion": self.catalog.config.version, "environment": self.environment,
                "inputAssetIds": list(sources), "previewRunId": str(preview_run.id) if preview_run else None,
                "cancelReason": None, "confirmReceipts": {}, "retryReceipts": {},
                "reviewer": self.catalog.config.reviewer.snapshot() if self.catalog.config.reviewer else None,
                "reviewThreshold": self.catalog.config.reviewThreshold,
                "admissionSettings": self.catalog.config.admission.model_dump(mode="json"),
                "cancelGraceSeconds": self.catalog.config.cancelGraceSeconds,
                "previewReferences": preview_refs or {}, "seedByStep": seeds or {},
                "finalMode": "reference_image" if preview_refs else "rerender" if preview_run else None,
                **({"intentTrace": decision.trace, "intentDecision": decision.model_dump(mode="json")} if decision else {}),
                "intentFeedback": [feedback] if feedback else []})
        db.add(run)
        await db.flush()
        # The Host freezes intent from existing deterministic planners, independently of
        # subsequent proposals. Models never receive a write/admission tool.
        message = intent_message or plan.goal
        if preview_run:
            await require_admission(db, preview_run)
            expected = plan  # final transformation above is code-owned and user-confirmed
        elif decision:
            expected = (plan_from_multi(decision, list(sources), self.catalog)
                if len(decision.steps or []) > 1 else plan_from_decision(decision, list(sources), self.catalog))
        else:
            expected = plan_intent(message, list(sources), self.catalog)
        readable = list(dict.fromkeys([*sources, *plan.sourceAssetIds]))
        result = await propose(db, run, Proposal(writer="rule", plan=plan.model_dump(mode="json")),
            IntentContract(message=message, expectedPlan=expected.model_dump(mode="json"),
                readableAssets=readable,
                inputReferences={item.id: (1 if (preview_refs or {}).get(item.id) else len(sources)) for item in plan.steps},
                writableNodes=[node for step in plan.steps for node in step.targetNodes],
                ambiguous=bool(decision and (decision.clarify or decision.certainty != "strong")),
                confirmedType=plan.goalType if intent_confirmed or preview_run else None),
            self.catalog, expected_version=run.state_version)
        if result.verdict != "approved":
            run.status = "waiting_input" if result.verdict == "clarify" else "failed"
            run.stage = "route" if result.verdict == "clarify" else "done"
            run.error = error("plan_review_failed", "计划未通过执行前审核")
            run.reply = "请明确希望生成图片还是视频。" if result.verdict == "clarify" else "计划未通过审核，未启动生成。"
            if result.verdict == "clarify":
                run.waiting = {"question": run.reply, "stage": "intent"}
                if decision:
                    clarified = self.intent._clarify(message, readable, request_id,
                        reference_snapshot(doc, list(selected)), copy.deepcopy(decision.trace), question=run.reply)
                    run.execution = {**run.execution, "intentDecision": clarified.model_dump(mode="json"),
                        "intentTrace": clarified.trace}
                    run.waiting = {**run.waiting, "options": [
                        {"id": item.id, "label": item.label} for item in clarified.clarify.options]}
            return {"run": await self._event(db, session, run, "host.plan_reviewed"), "replayed": False}
        steps = await self._steps(db, run)
        for asset in sources.values():
            db.add(AssetReference(asset_id=asset.id, owner_type="agent_run", owner_id=str(run.id)))
        if not waiting:
            await credits.reserve(db, run, steps, plan, self.catalog)
            await canvas.allocate(db, doc, run, steps, plan, self.catalog)
            self._wake(db, run)
            if any(self.catalog.resolve(item.capabilityId, item.tier).moderation == "required" for item in plan.steps):
                from .reviewer import enqueue_review
                await enqueue_review(db, self, run, session, kind="input", asset_ids=list(sources))
        projected = await self._event(db, session, run, "host.plan_created")
        return {"run": projected, "replayed": False}

    def _frozen_catalog(self, run):
        from .contracts import CatalogConfig
        return Catalog(CatalogConfig(version=run.execution["catalogVersion"],
            environment=run.execution.get("environment", "production"), enabled=True,
            admission=run.execution.get("admissionSettings", {}),
            maxConcurrency=run.execution["plan"]["maxConcurrency"],
            reviewer=run.execution.get("reviewer"), reviewMode=run.execution["plan"]["reviewMode"],
            reviewThreshold=run.execution.get("reviewThreshold", 0.7),
            capabilities=list(run.execution["catalog"].values())))

    async def confirm(self, workspace, principal, ident, request_id, approved):
        async with self.factory.begin() as db:
            doc, session, run = await self._locked(db, ident, workspace)
            if run.principal != principal:
                raise HostError("asset_forbidden", "只能确认自己的创作任务", status=403)
            receipts = run.execution["confirmReceipts"]
            if request_id in receipts:
                if receipts[request_id] != approved:
                    raise HostError("idempotency_conflict", "确认决定已提交，不能更改")
                return {"run": await self._project(db, run), "replayed": True}
            if run.status != "awaiting_confirm" or doc is None:
                raise HostError("confirmation_expired", "本次确认已失效")
            for node, source in run.snapshot.get("selectedSources", {}).items():
                current = doc.data.get("objects", {}).get(node)
                if current is None or any(current.get(key) != value for key, value in source.items()):
                    raise HostError("node_version_conflict", "参考节点已变化，请重新规划")
            if approved:
                await require_admission(db, run)
                frozen = self._frozen_catalog(run)
                plan, steps = MediaPlan.model_validate(run.execution["plan"]), await self._steps(db, run)
                if not run.execution.get("retryAwaiting"):
                    await credits.reserve(db, run, steps, plan, frozen)
                    await canvas.allocate(db, doc, run, steps, plan, frozen)
                run.status = "queued"
                self._wake(db, run)
                if any(frozen.resolve(item.capabilityId, item.tier).moderation == "required" for item in plan.steps):
                    from .reviewer import enqueue_review
                    await enqueue_review(db, self, run, session, kind="input", asset_ids=run.execution["inputAssetIds"])
            else:
                await self._cancel_locked(db, session, run, "user_cancel")
            run.execution = {**run.execution, "retryAwaiting": False, "pendingQuoteCredits": None, "confirmReceipts": {**receipts, request_id: approved}}
            return {"run": await self._event(db, session, run, "host.confirmed"), "replayed": False}

    async def final(self, workspace, principal, ident, raw):
        request = HostFinal.model_validate(raw)
        async with self.factory.begin() as db:
            doc, session, preview = await self._locked(db, ident, workspace)
            if preview.principal != principal:
                raise HostError("asset_forbidden", "只能生成自己的预览正片", status=403)
            fingerprint = hashlib.sha256(encoded({"preview": str(ident), **request.model_dump()}).encode()).hexdigest()
            previous = await db.scalar(select(AgentRun).where(AgentRun.session_id == session.id,
                AgentRun.client_request_id == request.clientRequestId))
            if previous:
                if previous.request_hash != fingerprint or previous.principal != principal:
                    raise HostError("idempotency_conflict", "同一正片请求标识不能更改来源")
                return {"run": await self._project(db, previous), "replayed": True}
            if doc is None or doc.revision != request.baseRevision:
                raise HostError("node_version_conflict", "画布已变化，请刷新后确认")
            if preview.status != "awaiting_user_final" or preview.execution["tier"] != "preview":
                raise HostError("confirmation_expired", "预览尚未完成或已生成正片")
            source_steps = await self._steps(db, preview)
            preview_assets = [child["assetId"] for step in source_steps for child in step.execution["children"]
                              if child["status"] == "completed"]
            if not preview_assets:
                raise HostError("plan_missing_source_assets", "预览没有可用产物")
            # Freeze both provenance assets and the original owned input references.
            sources = await self._sources(db, workspace, preview.execution["inputAssetIds"])
            output_sources = await self._sources(db, workspace, preview_assets)
            old_plan = MediaPlan.model_validate(preview.execution["plan"])
            preview_refs, seeds = {}, {}
            for step in source_steps:
                children = step.execution["children"]
                if all(child["assetId"] in output_sources and output_sources[child["assetId"]].kind == "image" for child in children):
                    preview_refs[step.key] = [child["assetId"] for child in children]
                seeds[step.key] = []
                for child in children:
                    node = doc.data.get("objects", {}).get(child["targetNode"])
                    if node is None or node.get("assetId") != child["assetId"]:
                        raise HostError("node_version_conflict", "样片节点已删除或替换，请重新确认来源")
                    job = await db.get(Job, uuid.UUID(child["jobId"]))
                    seeds[step.key].append(job.params.get("seed", -1))
            next_steps = [step.model_copy(update={"tier": "final", "title": ("正片 · " + step.title.replace("样片", ""))[:80],
                "capabilityId": "image_edit" if step.id in preview_refs else step.capabilityId,
                "targetNodes": ["host_" + uuid.uuid4().hex for _ in step.targetNodes]}) for step in old_plan.steps]
            next_plan = MediaPlan.model_validate({**old_plan.model_dump(mode="json"), "tier": "final",
                "sourceAssetIds": preview_assets, "steps": [step.model_dump(mode="json") for step in next_steps]})
            violations = validate_plan(next_plan, self.catalog)
            if violations:
                raise HostError(violations[0].code, "当前部署不能执行这份正片计划", status=422)
            result = await self._admit(db, doc, session, principal, request.clientRequestId, fingerprint,
                next_plan, sources, preview_run=preview, preview_refs=preview_refs, seeds=seeds)
            final_run = await db.get(AgentRun, uuid.UUID(result["run"]["id"]))
            for asset in output_sources.values():
                db.add(AssetReference(asset_id=asset.id, owner_type="agent_run", owner_id=str(final_run.id)))
            preview.status, preview.stage = "completed", "done"
            await self._event(db, session, preview, "host.final_requested")
            return result

    async def complete_canvas_action(self, workspace, principal, ident, raw):
        from .contracts import CanvasActionResult
        request = CanvasActionResult.model_validate(raw)
        async with self.factory.begin() as db:
            doc, session, run = await self._locked(db, ident, workspace)
            if run.principal != principal:
                raise HostError("asset_forbidden", "只能确认自己的抠图任务", status=403)
            action = run.execution.get("canvasAction")
            if not action or action.get("type") != "cutout":
                raise HostError("invalid_canvas_action", "此任务不是画布抠图", status=422)
            if run.status == "completed" and action.get("resultObjectId") == request.resultObjectId:
                return {"run": await self._project(db, run), "replayed": True}
            if run.status != "waiting_approval":
                raise HostError("confirmation_expired", "抠图任务已结束，请重新发起")
            if doc.revision != request.baseRevision:
                raise HostError("STALE_VERSION", "画布版本已变化，请同步后再次确认")
            asset_id = validate_cutout_result(action, request.resultObjectId, doc.data.get("objects", {}))
            sources = await self._sources(db, workspace, [asset_id])
            action = {**action, "resultObjectId": request.resultObjectId, "resultAssetId": asset_id}
            run.execution = {**run.execution, "canvasAction": action}
            run.status, run.stage, run.waiting = "completed", "done", None
            run.reply = "抠图已完成，透明背景图片已添加到画布。"
            for asset in sources.values():
                db.add(AssetReference(asset_id=asset.id, owner_type="agent_run", owner_id=str(run.id)))
            return {"run": await self._event(db, session, run, "host.canvas_completed"), "replayed": False}

    async def _cancel_locked(self, db, session, run, reason):
        if run.execution.get("canvasAction") and run.status == "waiting_approval":
            run.status, run.stage, run.waiting = "cancelled", "done", None
            run.reply = "已退出本次抠图，原图保留。"
            await self._event(db, session, run, "host.canvas_cancelled")
            return
        if run.status not in {*MEDIA_ACTIVE, "awaiting_confirm", "awaiting_user_final"}:
            return
        reserved = await db.scalar(select(HostCreditLedger.id).where(HostCreditLedger.run_id == run.id,
            HostCreditLedger.state == "reserved").limit(1))
        if run.status == "awaiting_confirm" and not reserved:
            run.status, run.stage = "cancelled", "done"
            run.execution = {**run.execution, "cancelReason": reason}
            await self._event(db, session, run, "host.cancelled")
            return
        run.status = "canceling"
        run.stop_requested_at = now()
        run.execution = {**run.execution, "cancelReason": reason,
            "cancelDeadline": run.execution.get("cancelDeadline") or (now() + timedelta(seconds=run.execution.get("cancelGraceSeconds", 120))).isoformat()}
        self._wake(db, run)
        await self._event(db, session, run, "host.cancel_requested")

    async def cancel(self, workspace, principal, ident, reason="user_cancel"):
        async with self.factory.begin() as db:
            _, session, run = await self._locked(db, ident, workspace)
            if run.principal != principal:
                raise HostError("asset_forbidden", "只能取消自己的创作任务", status=403)
            await self._cancel_locked(db, session, run, reason)
            return {"run": await self._project(db, run)}

    async def retry(self, workspace, principal, ident, raw):
        request = HostRetry.model_validate(raw)
        async with self.factory.begin() as db:
            doc, session, run = await self._locked(db, ident, workspace)
            if run.principal != principal:
                raise HostError("asset_forbidden", "只能重试自己的任务", status=403)
            receipts = run.execution["retryReceipts"]
            fingerprint = {"stepId": request.stepId, "index": request.index}
            if request.clientRequestId in receipts:
                if receipts[request.clientRequestId] != fingerprint:
                    raise HostError("idempotency_conflict", "同一重试标识不能改变目标")
                return {"run": await self._project(db, run), "replayed": True}
            if run.status != "failed" or doc is None:
                raise HostError("retry_not_allowed", "此运行当前不能重试")
            steps = await self._steps(db, run)
            target = next((step for step in steps if step.key == request.stepId), None)
            if target is None or request.index >= len(target.execution["children"]):
                raise HostError("retry_not_allowed", "失败步骤不存在")
            child = target.execution["children"][request.index]
            if child["status"] != "failed" or child["errorCode"] in {"content_blocked", "capability_unavailable", "asset_forbidden", "node_missing", "node_version_conflict", "UPSTREAM_AUTH", "INVALID_PARAM"}:
                raise HostError("retry_not_allowed", "此失败需要调整目标后重新创作")
            await require_admission(db, run)
            frozen, plan = self._frozen_catalog(run), MediaPlan.model_validate(run.execution["plan"])
            cap = frozen.resolve(target.execution["spec"]["capabilityId"], plan.tier)
            if child["attempt"] >= cap.binding.maxAttempts:
                raise HostError("retry_limit", "已达到该步骤重试上限")
            restart = [(target, request.index)]
            # Reopen only skipped descendants, never rerun successful siblings.
            affected = {target.key}
            while True:
                descendants = {step.key for step in steps if set(step.depends_on) & affected}
                if descendants <= affected:
                    break
                affected.update(descendants)
            restart.extend((step, item["index"]) for step in steps if step.key in affected and step.key != target.key
                           for item in step.execution["children"] if item["status"] == "skipped")
            total = sum(frozen.resolve(step.execution["spec"]["capabilityId"], plan.tier).tiers[plan.tier].credits
                        for step, _ in restart)
            committed = list(await db.scalars(select(HostCreditLedger).where(HostCreditLedger.run_id == run.id)))
            budget_used = sum(row.quoted if row.state == "reserved" else row.charged for row in committed)
            if budget_used + total > frozen.config.admission.maxCredits:
                raise HostError("credit_budget_exceeded", "重试将超出本次运行的积分预算", status=409)
            account = await credits.account_locked(db, workspace)
            if account.available < total:
                raise HostError("quota_exceeded", "额度不足，无法重试本步骤", status=402)
            account.available -= total
            retry_specs = []
            for step, index in restart:
                execution = copy.deepcopy(step.execution)
                item = execution["children"][index]
                node = doc.data.get("objects", {}).get(item["targetNode"])
                if node is None or node.get("createdByRunId") != str(run.id) or node.get("kind") != "error" or node.get("assetId"):
                    raise HostError("node_version_conflict", "重试位置已被修改，请重新创作")
                attempt = item["attempt"] + 1
                binding = frozen.resolve(execution["spec"]["capabilityId"], plan.tier)
                old = await db.scalar(select(HostCreditLedger).where(HostCreditLedger.run_id == run.id,
                    HostCreditLedger.step_id == step.id, HostCreditLedger.child_index == index, HostCreditLedger.attempt == item["attempt"]))
                key = hashlib.sha256(f"{old.idempotency_key}:{attempt}".encode()).hexdigest()
                db.add(HostCreditLedger(workspace_id=workspace, run_id=run.id, step_id=step.id, child_index=index,
                    attempt=attempt, idempotency_key=key, quoted=binding.tiers[plan.tier].credits,
                    charged=0, refunded=0, state="reserved", details={**old.details, "reviewCallId": None,
                    "hardChecks": None, "attempt": attempt, "retryOf": str(old.id)}))
                item.update(status="pending", attempt=attempt, jobId=None, assetId=None, errorCode=None)
                step.execution, step.status = execution, "pending"
                original = next(spec for spec in plan.steps if spec.id == step.key)
                retry_specs.append(original.model_copy(update={"fanOut": 1, "targetNodes": [item["targetNode"]],
                    "variants": [original.variants[index]] if original.variants else []}))
            await canvas.allocate(db, doc, run, steps, plan.model_copy(update={"steps": retry_specs}), frozen)
            waiting = total > self.catalog.config.confirmCreditsAbove
            run.status, run.stage, run.stop_requested_at, run.error = "awaiting_confirm" if waiting else "queued", "execute", None, None
            run.execution = {**run.execution, "quoteCredits": run.execution["quoteCredits"] + total,
                "retryAwaiting": waiting, "pendingQuoteCredits": total if waiting else None,
                "retryReceipts": {**receipts, request.clientRequestId: fingerprint}}
            if not waiting:
                self._wake(db, run)
            return {"run": await self._event(db, session, run, "host.retry_requested"), "replayed": False}

    async def _inspect_completed(self, ident):
        from .inspection import inspect
        pending = []
        async with self.factory() as db:
            run = await db.get(AgentRun, ident)
            if run is None or run.phase != "media" or run.status not in MEDIA_ACTIVE:
                return {}
            if run.status == "canceling":
                return {}
            frozen = self._frozen_catalog(run)
            rows = list(await db.scalars(select(HostCreditLedger).where(HostCreditLedger.run_id == ident,
                HostCreditLedger.state == "reserved", HostCreditLedger.job_id.is_not(None))))
            for ledger in rows:
                job = await db.get(Job, ledger.job_id)
                if job and job.status == "completed":
                    asset = await db.get(Asset, uuid.UUID(job.phase_payload["assetId"]))
                    spec = frozen.resolve(ledger.details["capabilityId"], ledger.details["tier"]).tiers[ledger.details["tier"]]
                    if asset:
                        pending.append((str(job.id), asset, spec))
        receipts = {}
        for job_id, asset, spec in pending:
            receipts[job_id] = await inspect(asset, spec, self.media_root) if self.media_root else {
                "passed": False, "code": "media_probe_unavailable", "message": "媒体质检存储未配置", "sha256": asset.sha256}
        return receipts

    async def tick(self, ident):
        hard_receipts = await self._inspect_completed(ident)
        cancel_jobs = []
        async with self.factory.begin() as db:
            # Same order as media admission: global queue lock before Document.
            await db.execute(text("SELECT pg_advisory_xact_lock(72140901)"))
            doc, session, run = await self._locked(db, ident)
            if run.status not in MEDIA_ACTIVE:
                return {"skipped": True}
            steps = await self._steps(db, run)
            plan = MediaPlan.model_validate(run.execution["plan"])
            catalog = self._frozen_catalog(run)
            ledgers = list(await db.scalars(select(HostCreditLedger).where(HostCreditLedger.run_id == run.id)
                .order_by(HostCreditLedger.step_id, HostCreditLedger.child_index).with_for_update()))
            ledger_map = {(row.step_id, row.child_index, row.attempt): row for row in ledgers}
            if run.status != "canceling":
                try:
                    await require_admission(db, run)
                except HostError as exc:
                    run.status = "needs_attention"
                    run.error = error(exc.code, exc.message)
                    run.next_run_at = run.dispatched_until = None
                    return await self._event(db, session, run, "host.admission_required")
            if doc is None or session.closed_at:
                run.status = "canceling"
                run.execution = {**run.execution, "cancelReason": "document_deleted"}
            moderation = None
            if run.execution.get("inputReviewCallId"):
                from .reviewer import result_for
                moderation = await result_for(db, run.execution["inputReviewCallId"])
            for step in steps:
                execution = copy.deepcopy(step.execution)
                for child in execution["children"]:
                    ledger = ledger_map[(step.id, child["index"], child["attempt"])]
                    job = await db.get(Job, uuid.UUID(child["jobId"]), with_for_update=True) if child["jobId"] else None
                    if child["status"] in CHILD_TERMINAL:
                        continue
                    cancelled = run.status == "canceling"
                    if not cancelled and doc:
                        lease = await db.get(HostNodeLease, (doc.id, child["targetNode"]), with_for_update=True)
                        node = doc.data.get("objects", {}).get(child["targetNode"])
                        if (node is None or lease is None or lease.run_id != run.id or
                                node.get("busyRunId") != str(run.id) or int(node.get("assetVersion", 0)) != lease.expected_version):
                            child.update(status="failed", errorCode="node_missing" if node is None else "lease_lost")
                            if job and job.status not in {"completed", "failed"}:
                                cancel_jobs.append((job.id, run.workspace_id))
                            else:
                                await credits.settle(db, ledger, job, error_code=child["errorCode"])
                            await canvas.release(db, doc, run, child["targetNode"], child["errorCode"])
                            continue
                        # Renew the same fenced owner through generation and content review.
                        lease.expires_at = now() + timedelta(seconds=120)
                    if moderation and (moderation["contentBlocked"] or not moderation["passed"]):
                        child.update(status="blocked", errorCode="content_blocked" if moderation["contentBlocked"] else "moderation_unavailable")
                        await credits.settle(db, ledger, error_code=child["errorCode"])
                        if doc:
                            await canvas.release(db, doc, run, child["targetNode"], child["errorCode"])
                        continue
                    if job and job.status in {"queued", "running", "unknown"}:
                        child["status"] = "unknown" if job.status == "unknown" else "running" if job.status != "queued" else "queued"
                        if job.status == "unknown":
                            child["errorCode"] = "provider_result_unknown"
                        if cancelled:
                            cancel_jobs.append((job.id, run.workspace_id))
                            from datetime import datetime
                            deadline = run.execution.get("cancelDeadline")
                            if deadline and datetime.fromisoformat(deadline) <= now():
                                child.update(status="cancelled", errorCode="cancel_timeout")
                                # Keep the reservation until the provider has a terminal receipt.
                                if doc:
                                    await canvas.release(db, doc, run, child["targetNode"], "cancel_timeout")
                                continue
                        lease = await db.get(HostNodeLease, (uuid.UUID(run.snapshot["id"]), child["targetNode"]))
                        if lease and lease.run_id == run.id:
                            lease.expires_at = now() + timedelta(seconds=120)
                        continue
                    if cancelled or (job and job.status == "failed"):
                        child["status"] = "cancelled" if cancelled else "blocked" if job.error_code == "CONTENT_BLOCKED" else "failed"
                        child["errorCode"] = "run_superseded" if cancelled else (job.error_code or "provider_failed")
                        await credits.settle(db, ledger, job, cancelled=cancelled, error_code=child["errorCode"])
                        if doc:
                            await canvas.release(db, doc, run, child["targetNode"], child["errorCode"])
                    elif job and job.status == "completed":
                        await credits.settle(db, ledger, job)
                        asset = await db.get(Asset, uuid.UUID(job.phase_payload["assetId"]))
                        cap = catalog.resolve(execution["spec"]["capabilityId"], plan.tier)
                        receipt = hard_receipts.get(str(job.id), (ledger.details.get("hardChecks") or {"passed": False, "code": "artifact_invalid"}))
                        ledger.details = {**ledger.details, "hardChecks": receipt}
                        if not receipt["passed"]:
                            child.update(status="failed", errorCode=receipt["code"])
                        elif asset is None or asset.kind != cap.outputType:
                            child.update(status="failed", errorCode="artifact_type_mismatch")
                        else:
                            quality_issue = None
                            if plan.reviewMode != "off":
                                from .reviewer import enqueue_review, result_for
                                call_id = ledger.details.get("reviewCallId")
                                if not call_id:
                                    await enqueue_review(db, self, run, session, kind="output", asset_ids=[str(asset.id)], ledger=ledger, step=step)
                                    child["status"] = "reviewing"
                                    continue
                                verdict = await result_for(db, call_id)
                                if verdict is None:
                                    child["status"] = "reviewing"
                                    continue
                                ledger.details = {**ledger.details, "reviewResult": verdict}
                                passed = verdict["passed"] and verdict["score"] >= run.execution.get("reviewThreshold", 0.7)
                                if verdict["contentBlocked"]:
                                    asset.details = {**asset.details, "hostBlocked": True}
                                    job.status, job.error_code = "failed", "CONTENT_BLOCKED"
                                    child.update(status="blocked", errorCode="content_blocked")
                                    if doc:
                                        await canvas.release(db, doc, run, child["targetNode"], "content_blocked")
                                    continue
                                if not passed and plan.reviewMode == "hard":
                                    child.update(status="failed", errorCode="review_hard_fail")
                                    if doc:
                                        await canvas.release(db, doc, run, child["targetNode"], "review_hard_fail")
                                    continue
                                if not passed:
                                    quality_issue = "；".join(verdict["issues"]) or "质检结果需要人工确认"
                            try:
                                await canvas.write_asset(db, doc, run, child["targetNode"], asset, quality_issue=quality_issue)
                                child.update(status="completed", assetId=str(asset.id))
                            except HostError as exc:
                                child.update(status="failed", errorCode=exc.code)
                        if child["status"] != "completed" and doc:
                            await canvas.release(db, doc, run, child["targetNode"], child["errorCode"])
                step.execution = execution
                statuses = {child["status"] for child in execution["children"]}
                step.status = ("completed" if statuses == {"completed"} else "running" if statuses & {"queued", "running", "reviewing", "unknown"}
                    else "blocked" if statuses <= CHILD_TERMINAL else "pending")
                step.summary = f"{sum(child['status'] == 'completed' for child in execution['children'])}/{len(execution['children'])} 完成"
            # Dependency failure is explicit; never dispatch a replacement image.
            by_key = {step.key: step for step in steps}
            sequential_failed = bool(len((run.execution.get("intentDecision") or {}).get("steps") or []) > 1
                and any(step.status == "blocked" for step in steps))
            for step in steps:
                if step.status == "pending" and (sequential_failed or any(by_key[dep].status == "blocked" for dep in step.depends_on)):
                    execution = copy.deepcopy(step.execution)
                    for child in execution["children"]:
                        if child["status"] == "pending":
                            child.update(status="skipped", errorCode="dependency_failed")
                            await credits.settle(db, ledger_map[(step.id, child["index"], child["attempt"])], error_code="dependency_failed")
                            if doc:
                                await canvas.release(db, doc, run, child["targetNode"], "dependency_failed")
                    step.execution, step.status, step.summary = execution, "blocked", "依赖失败，未执行"
            if run.execution.get("intentDecision"):
                execution = copy.deepcopy(run.execution)
                trace = execution["intentTrace"]
                results = []
                trace.pop("step_fail_index", None)
                for index, semantic in enumerate(execution["intentDecision"].get("steps") or []):
                    members = [step for step in steps if step.execution["spec"].get("intentStepId") == semantic["id"]]
                    if not members and len(execution["intentDecision"].get("steps") or []) == 1:
                        members = steps
                    statuses = {step.status for step in members}
                    state = "completed" if statuses == {"completed"} else "blocked" if "blocked" in statuses else "running" if "running" in statuses else "pending"
                    results.append({"id": semantic["id"], "l2": semantic["l2"], "status": state,
                        "asset_ids": [child["assetId"] for member in members for child in member.execution["children"] if child.get("assetId")],
                        "error_codes": sorted({child["errorCode"] for member in members for child in member.execution["children"] if child.get("errorCode")})})
                    if state == "blocked" and "step_fail_index" not in trace:
                        trace["step_fail_index"] = index
                trace["step_results"] = results
                execution["intentTrace"] = trace
                run.execution = execution
            children = [child for step in steps for child in step.execution["children"]]
            if all(child["status"] in CHILD_TERMINAL for child in children):
                failed = [child for child in children if child["status"] != "completed"]
                run.status = ("cancelled" if run.status == "canceling" else "blocked" if any(child["status"] == "blocked" for child in failed)
                    else "failed" if failed else "awaiting_user_final" if plan.tier == "preview" else "completed")
                run.stage = "done"
                run.error = error(failed[0]["errorCode"], "部分任务未完成，成功作品已保留") if failed else None
                run.reply = f"{sum(child['status'] == 'completed' for child in children)}/{len(children)} 个创作步骤完成。"
                run.next_run_at = run.dispatched_until = None
                if not failed and run.execution.get("intentDecision") and not run.execution.get("intentSuccessLogged"):
                    execution = copy.deepcopy(run.execution)
                    decision = execution["intentDecision"]
                    execution.setdefault("intentFeedback", []).append(candidate("task_success", run.message,
                        decision["l2"], run.client_request_id, {"document_id": run.snapshot["id"],
                            "nodes": run.snapshot.get("selectedSources", {})}, user_action="completed",
                        steps=decision.get("steps")))
                    execution["intentSuccessLogged"] = True
                    run.execution = execution
            elif run.status != "canceling":
                from backend.errors import QueueFullError
                try:
                    await self._dispatch(db, doc, run, steps, plan, catalog, ledger_map)
                    run.error = None
                except QueueFullError:
                    run.error = error("rate_limited", "正在等待可用队列容量，请稍后")
                if any(child["status"] == "unknown" for child in children):
                    run.status = "needs_attention"
                    run.error = error("provider_result_unknown", "生成结果待确认，不会重复提交；可取消并等待对账")
                else:
                    run.status = "running"
            if run.status in MEDIA_ACTIVE:
                run.next_run_at = now() + timedelta(seconds=3)
                run.dispatched_until = None
            await self._event(db, session, run, "host.progress")
            result = {"status": run.status}
        # Provider cancellation uses existing fenced jobs after releasing all Host locks.
        if cancel_jobs:
            from backend.services.execution import ExecutionStore
            for job_id, workspace in cancel_jobs:
                await ExecutionStore(self.factory).request_cancel(job_id, workspace)
        return result

    async def _dispatch(self, db, doc, run, steps, plan, catalog, ledgers):
        await require_admission(db, run)
        if run.execution.get("inputReviewCallId"):
            from .reviewer import result_for
            verdict = await result_for(db, run.execution["inputReviewCallId"])
            if verdict is None or not verdict["passed"] or verdict["contentBlocked"]:
                return
        active = sum(child["status"] in {"queued", "running", "reviewing", "unknown"} for step in steps for child in step.execution["children"])
        capacity = plan.maxConcurrency - active
        by_key = {step.key: step for step in steps}
        sources = await self._sources(db, run.workspace_id, run.execution["inputAssetIds"])
        for step in steps:
            if capacity <= 0 or step.status != "pending" or any(by_key[dep].status != "completed" for dep in step.depends_on):
                continue
            execution = copy.deepcopy(step.execution)
            spec = execution["spec"]
            cap = catalog.resolve(spec["capabilityId"], plan.tier)
            binding, tier = cap.binding, cap.tiers[plan.tier]
            # PG admission serializes all Host bindings, including different canvases.
            # Count persisted queued/running jobs, not transient Worker tasks.
            scope = f"{cap.id}:{binding.version}"
            busy = await db.scalar(select(func.count()).select_from(Job).where(
                Job.status.in_(["queued", "running", "unknown"]), Job.params["hostBinding"].astext == scope))
            binding_capacity = binding.maxConcurrency - busy
            for child in execution["children"]:
                if capacity <= 0 or binding_capacity <= 0 or child["status"] != "pending":
                    continue
                ledger = ledgers[(step.id, child["index"], child["attempt"])]
                inputs = [by_key[item["stepId"]].execution["children"][item.get("index", 0)]["assetId"]
                          for item in spec["inputFrom"]]
                if not inputs and run.execution.get("previewReferences", {}).get(step.key):
                    inputs = [run.execution["previewReferences"][step.key][child["index"]]]
                input_rows = await self._sources(db, run.workspace_id, inputs) if inputs else sources
                if binding.engine == "provider" and len(input_rows) > cap.maxRefImages:
                    raise HostError("spec_not_allowed", "参考图数量超出能力目录限制", status=400)
                params = {"prompt": spec["variants"][child["index"]] if spec["variants"] else spec["prompt"],
                    "kind": cap.outputType, "width": tier.width, "height": tier.height,
                    "durationSec": tier.durationSec, "fps": tier.fps, "batchCount": 1,
                    "denoise": tier.denoise if input_rows else 1,
                    "seed": run.execution.get("seedByStep", {}).get(step.key, [-1] * len(execution["children"]))[child["index"]],
                    "documentId": str(doc.id), "clientRef": child["targetNode"],
                    "hostBinding": scope, "hostRunId": str(run.id), "hostAdmissionId": run.execution["admissionId"], "hostStepId": step.key,
                    "hostChildIndex": child["index"], "hostTier": plan.tier, "hostPrincipal": run.principal, "hostEnvironment": run.execution.get("environment", "production"),
                    "sourceAssetIds": plan.sourceAssetIds, "referenceAssetIds": list(input_rows),
                    "traceId": str(run.id), "requestId": ledger.idempotency_key}
                if binding.engine == "provider":
                    snapshot = binding.providerSnapshot
                    params["model"] = snapshot["cloudModel" if cap.outputType == "image" else "videoModel"]
                    if input_rows:
                        asset = next(iter(input_rows.values()))
                        params["referenceStorage"] = {"key": asset.storage_key, "ext": asset.ext}
                    kind = cap.outputType
                else:
                    snapshot, kind = {"provider": "ffmpeg", "configVersion": binding.version}, "export"
                    params.update(model="frame-interpolation", hostFrameComposition=True,
                        frameSources=[{"key": input_rows[asset_id].storage_key, "assetId": asset_id}
                                      for asset_id in inputs])
                ledger.details = {**ledger.details, "admittedInputs": {
                    ident: {"kind": asset.kind, "sha256": asset.sha256, "bytes": asset.bytes, "storage": asset.storage_key}
                    for ident, asset in input_rows.items()}, "admittedRequestHash": digest({"params": params,
                    "kind": kind, "provider": snapshot, "principal": run.principal})}
                await db.flush()
                accepted = await accept_generation(self.factory, session=db, workspace_id=run.workspace_id,
                    request_id=ledger.idempotency_key, request_hash=hashlib.sha256(encoded(params).encode()).hexdigest(),
                    params=params, kind=kind, provider_snapshot=snapshot, principal=run.principal)
                ledger.job_id = accepted["jobId"]
                job = await db.get(Job, ledger.job_id)
                ledger.details = {**ledger.details, "admittedJobHash": digest({"params": job.params,
                    "provider": job.provider_snapshot, "kind": job.kind})}
                child.update(status="queued", jobId=str(ledger.job_id))
                capacity -= 1
                binding_capacity -= 1
            step.execution = execution
            if any(child["status"] in {"queued", "running"} for child in execution["children"]):
                step.status = "running"

    async def reconcile_terminal_credits(self, limit=100):
        async with self.factory() as db:
            ids = list(await db.scalars(select(HostCreditLedger.id).join(AgentRun, AgentRun.id == HostCreditLedger.run_id)
                .join(Job, Job.id == HostCreditLedger.job_id).where(HostCreditLedger.state == "reserved",
                    AgentRun.status.in_(["cancelled", "failed", "blocked"]), Job.status.in_(["completed", "failed"]))
                .order_by(HostCreditLedger.created_at).limit(limit)))
        for ident in ids:
            async with self.factory.begin() as db:
                row = await db.get(HostCreditLedger, ident)
                _, session, run = await self._locked(db, row.run_id)
                ledger = await db.get(HostCreditLedger, ident, with_for_update=True)
                job = await db.get(Job, ledger.job_id, with_for_update=True)
                if job.status in {"completed", "failed"}:
                    await credits.settle(db, ledger, job, cancelled=run.status == "cancelled")
                    await self._event(db, session, run, "host.late_credit_receipt")
        return len(ids)

    async def recover_due(self, limit=100):
        await self.reconcile_terminal_credits(limit)
        async with self.factory() as db:
            ids = list(await db.scalars(select(AgentRun.id).where(AgentRun.phase == "media",
                AgentRun.status.in_(MEDIA_ACTIVE),
                (AgentRun.next_run_at.is_(None) | (AgentRun.next_run_at <= now())),
                (AgentRun.dispatched_until.is_(None) | (AgentRun.dispatched_until <= now())))
                .order_by(AgentRun.created_at).limit(limit)))
        for ident in ids:
            async with self.factory.begin() as db:
                _, _, run = await self._locked(db, ident)
                if run.status in MEDIA_ACTIVE and (run.dispatched_until is None or run.dispatched_until <= now()):
                    self._wake(db, run)
        return len(ids)


async def project_media(db, run, value):
    if run.execution.get("planReview"):
        value["planReview"] = run.execution["planReview"]
    if run.execution.get("intentTrace"):
        value["intentTrace"] = run.execution["intentTrace"]
    if run.execution.get("canvasAction"):
        value["canvasAction"] = run.execution["canvasAction"]
    if not run.execution.get("plan"):
        value["media"] = None
        return value
    from pydantic import ValidationError
    try:
        MediaPlan.model_validate(run.execution["plan"])
    except ValidationError:
        value["media"] = None
        return value
    steps = list(await db.scalars(select(AgentStep).where(AgentStep.run_id == run.id,
        AgentStep.plan_version == run.plan["version"]).order_by(AgentStep.ordinal)))
    ledgers = list(await db.scalars(select(HostCreditLedger).where(HostCreditLedger.run_id == run.id)))
    children = [child for step in steps for child in (step.execution or {}).get("children", [])]
    doc = await db.get(Document, uuid.UUID(run.snapshot["id"]))
    value["media"] = {"tier": run.execution["tier"], "goalType": run.execution["plan"]["goalType"],
        "previewRunId": run.execution.get("previewRunId"), "quoteCredits": run.execution["quoteCredits"], "pendingQuoteCredits": run.execution.get("pendingQuoteCredits"),
        "chargedCredits": sum(row.charged for row in ledgers), "refundCredits": sum(row.refunded for row in ledgers),
        "completed": sum(child["status"] == "completed" for child in children), "total": len(children),
        "documentRevision": doc.revision if doc else None,
        "cancelReason": run.execution.get("cancelReason"), "finalMode": run.execution.get("finalMode"),
        "children": [{"stepId": step.key, "title": step.title,
            "intentStepId": step.execution["spec"].get("intentStepId"),
            "retryCredits": run.execution["catalog"][step.execution["spec"]["capabilityId"]]["tiers"][run.execution["tier"]]["credits"],
            **{key: child.get(key) for key in ("index", "status", "assetId", "errorCode", "targetNode", "attempt")}}
            for step in steps for child in step.execution["children"]]}
    return value
