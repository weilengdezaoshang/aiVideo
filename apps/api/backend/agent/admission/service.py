"""Review once, persist receipts, then authorize effects against immutable inputs."""
from __future__ import annotations

import copy
import json
import uuid
from sqlalchemy import select, update
from pydantic import ValidationError

from backend.agent.media.contracts import MediaPlan

from backend.agent.orm import (AgentRun, AgentModelCall, AgentPlanVersion, AgentStep,
    AgentPlanReview, AgentExecutionAdmission)
from backend.infrastructure.orm import Asset, Outbox
from backend.agent.media.catalog import HostError
from .policy import IntentContract, Proposal, Issue, Review, digest, review


def parse_id(value):
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return None


def bindings(run):
    return {key: run.execution.get(key) for key in ("catalog", "catalogVersion", "environment",
        "inputAssetIds", "previewReferences", "seedByStep", "reviewer", "reviewThreshold", "admissionSettings")}


def asset_identity(asset):
    return {"kind": asset.kind, "sha256": asset.sha256, "bytes": asset.bytes, "storage": asset.storage_key}


def model_contains_plan(response, plan):
    candidates = [response.get("content")]
    candidates.extend(call.get("function", {}).get("arguments") for call in response.get("tool_calls", []))
    for raw in candidates:
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            continue
        if value == plan or isinstance(value, dict) and value.get("plan") == plan:
            return True
    return False


async def propose(db, run, proposal: Proposal, intent: IntentContract, catalog, *, expected_version: int):
    """Caller holds Document -> Session -> Run locks; CAS also rejects stale writers.

    There are deliberately no model tools or HTTP endpoints for issuing intent contracts.
    All proposal writers use exactly this gate. Rejected proposals remain committed records.
    """
    actual_version = await db.scalar(select(AgentRun.state_version).where(AgentRun.id == run.id))
    if actual_version != expected_version:
        raise HostError("STALE_VERSION", "计划版本已变化，请重新读取", status=409)
    previous = await db.scalar(select(AgentPlanVersion).where(AgentPlanVersion.run_id == run.id)
        .order_by(AgentPlanVersion.version.desc()).limit(1))
    if (previous and previous.proposal == proposal.model_dump(mode="json")
            and previous.intent == intent.model_dump(mode="json")
            and {key: value for key, value in previous.bindings.items() if key != "assetVersions"} == bindings(run)):
        record = await db.scalar(select(AgentPlanReview).where(AgentPlanReview.plan_id == previous.id))
        return Review.model_validate(record.result)
    if previous:
        from backend.agent.orm import HostCreditLedger
        effect = await db.scalar(select(HostCreditLedger.id).where(HostCreditLedger.run_id == run.id).limit(1))
        if effect or previous.intent != intent.model_dump(mode="json") or {key: value for key, value in previous.bindings.items() if key != "assetVersions"} != bindings(run):
            raise HostError("plan_revision_forbidden", "已有执行或需求约束变化，请创建新运行", status=409)
    if run.status == "blocked" or intent.blocked:
        intent = intent.model_copy(update={"blocked": True})
    if proposal.writer in {"llm", "small", "helper"}:
        model_id = parse_id(proposal.modelCallId)
        call = await db.get(AgentModelCall, model_id) if model_id else None
        if call is None or call.run_id != run.id or call.status != "completed" or call.response is None:
            raise HostError("model_output_not_persisted", "模型输出必须先落库", status=409)
        if not model_contains_plan(call.response, proposal.plan):
            raise HostError("model_output_mismatch", "单子与落库的模型输出不一致", status=409)
    changed = await db.execute(update(AgentRun).where(AgentRun.id == run.id,
        AgentRun.state_version == expected_version).values(state_version=expected_version + 1)
        .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise HostError("STALE_VERSION", "计划版本已变化，请重新读取", status=409)
    run.state_version = expected_version + 1
    frozen = copy.deepcopy(bindings(run))
    asset_versions, asset_issues = {}, []
    for ident in sorted(set(intent.readableAssets)):
        try:
            asset = await db.get(Asset, uuid.UUID(ident), with_for_update=True)
        except ValueError:
            asset = None
        if asset is None or asset.workspace_id != run.workspace_id or asset.details.get("hostBlocked"):
            asset_issues.append(Issue(gate="legal", code="asset_forbidden", path=ident))
        else:
            asset_versions[ident] = asset_identity(asset)
    frozen = {**frozen, "assetVersions": asset_versions}
    try:
        canonical_plan = MediaPlan.model_validate(proposal.plan).model_dump(mode="json")
    except ValidationError:
        canonical_plan = proposal.plan
    row = AgentPlanVersion(run_id=run.id, version=(previous.version if previous else 0) + 1, writer=proposal.writer,
        proposal=proposal.model_dump(mode="json"), content_hash=digest(canonical_plan),
        intent=intent.model_dump(mode="json"), bindings=frozen)
    db.add(row)
    await db.flush()
    result = review(proposal, intent, catalog, catalog.config.admission)
    if asset_issues:
        result = result.model_copy(update={"verdict": "rejected", "issues": [*result.issues, *asset_issues],
            "checks": {**result.checks, "legal": False}})
    record = AgentPlanReview(plan_id=row.id, result=result.model_dump(mode="json"),
        input_hash=digest({"intent": row.intent, "bindings": frozen, "settings": catalog.config.admission.model_dump()}))
    db.add(record)
    await db.flush()
    admission = None
    if result.verdict == "approved":
        admission = AgentExecutionAdmission(review_id=record.id, plan_id=row.id, content_hash=row.content_hash)
        db.add(admission)
        await db.flush()
    if result.verdict == "approved":
        for ordinal, spec in enumerate(canonical_plan["steps"]):
            db.add(AgentStep(run_id=run.id, plan_version=row.version, key=spec["id"], ordinal=ordinal,
                title=spec["title"], intent=spec["prompt"], allowed_tools=[spec["capabilityId"]],
                depends_on=spec["dependsOn"], done_when="有效产物写入目标画布节点", status="pending",
                evidence_refs=[], retries=0, execution={"spec": spec, "children": [
                    {"index": index, "status": "pending", "attempt": 1, "jobId": None,
                     "assetId": None, "errorCode": None, "targetNode": node}
                    for index, node in enumerate(spec["targetNodes"])]}))
        if previous:
            run.status = "awaiting_confirm"
        await db.flush()
    if result.verdict != "approved":
        run.status = "waiting_input" if result.verdict == "clarify" else "failed"
        run.next_run_at = run.dispatched_until = None
    run.plan = {**(run.plan or {}), "version": row.version}
    run.execution = {**run.execution, "plan": canonical_plan, "planReview": result.model_dump(mode="json"),
        "planVersionId": str(row.id), "admissionId": str(admission.id) if admission else None}
    # Reuse the existing routed wakeup. Consumers read committed status; rejection cannot execute.
    db.add(Outbox(aggregate_type="agent", aggregate_id=str(run.id), event_type="agent.due",
        payload={"runId": str(run.id)}))
    return result


async def require_admission(db, run):
    ident = parse_id((run.execution or {}).get("admissionId"))
    admission = await db.get(AgentExecutionAdmission, ident) if ident else None
    plan = await db.get(AgentPlanVersion, admission.plan_id) if admission else None
    record = await db.get(AgentPlanReview, admission.review_id) if admission else None
    if (plan is None or record is None or plan.run_id != run.id or record.plan_id != plan.id
            or record.result.get("verdict") != "approved"
            or run.execution.get("planVersionId") != str(plan.id)
            or (run.plan or {}).get("version") != plan.version
            or admission.content_hash != plan.content_hash
            or digest(run.execution.get("plan")) != plan.content_hash
            or bindings(run) != {key: value for key, value in plan.bindings.items() if key != "assetVersions"}):
        raise HostError("admission_required", "计划未审核或已变化，请重新规划", status=409)
    normalized = MediaPlan.model_validate(plan.proposal["plan"]).model_dump(mode="json")
    specs = {step["id"]: step for step in normalized["steps"]}
    steps = list(await db.scalars(select(AgentStep).where(AgentStep.run_id == run.id,
        AgentStep.plan_version == plan.version)))
    if {step.key for step in steps} != set(specs):
        raise HostError("admission_required", "执行步骤不完整", status=409)
    for step in steps:
        spec = specs.get(step.key)
        children = (step.execution or {}).get("children", [])
        if (spec is None or step.execution.get("spec") != spec
                or step.depends_on != spec["dependsOn"] or len(children) != spec["fanOut"]
                or [child.get("targetNode") for child in children] != spec["targetNodes"]
                or [child.get("index") for child in children] != list(range(spec["fanOut"]))):
            raise HostError("admission_required", "执行步骤与已审核计划不一致", status=409)
    for ident in sorted(set(plan.intent["readableAssets"])):
        asset = await db.get(Asset, uuid.UUID(ident), with_for_update=True)
        if (asset is None or asset.workspace_id != run.workspace_id or asset.details.get("hostBlocked")
                or asset_identity(asset) != plan.bindings.get("assetVersions", {}).get(ident)):
            raise HostError("asset_forbidden", "审核后参考资产已不可用或版本变化", status=409)
    return plan


async def authorize_submission(db, workspace, params, request_id, principal, kind, provider_snapshot):
    """Only a reserved, exact worker-built request can cross the generic Job boundary."""
    from backend.agent.orm import AgentSession, HostCreditLedger
    raw = params.get("hostRunId")
    if not raw:
        raise HostError("admission_required", "Agent 任务缺少运行标识", status=409)
    ident = parse_id(raw)
    session_id = await db.scalar(select(AgentRun.session_id).where(AgentRun.id == ident))
    if session_id is None:
        raise HostError("admission_required", "Agent 运行不存在", status=409)
    await db.get(AgentSession, session_id, with_for_update=True)
    run = await db.get(AgentRun, ident, with_for_update=True)
    await require_admission(db, run)
    ledger = await db.scalar(select(HostCreditLedger).where(HostCreditLedger.run_id == ident,
        HostCreditLedger.idempotency_key == request_id).with_for_update())
    fingerprint = digest({"params": params, "kind": kind, "provider": provider_snapshot, "principal": principal})
    if (run.workspace_id != workspace or run.principal != principal or run.status not in {"queued", "running"}
            or params.get("hostAdmissionId") != run.execution["admissionId"]
            or ledger is None or ledger.state != "reserved" or ledger.job_id is not None
            or ledger.details.get("admittedRequestHash") != fingerprint):
        raise HostError("admission_required", "任务与已审核计划或积分预占不一致", status=409)


async def lock_job_run(db, params):
    """Preserve Document -> Session -> Run -> Job order at paid submission."""
    from backend.agent.orm import AgentSession
    from backend.infrastructure.orm import Document
    ident = uuid.UUID(params["hostRunId"])
    row = await db.get(AgentRun, ident)
    if row is None:
        raise HostError("admission_required", "Agent 运行不存在", status=409)
    await db.get(Document, uuid.UUID(row.snapshot["id"]), with_for_update=True)
    await db.get(AgentSession, row.session_id, with_for_update=True)
    return await db.get(AgentRun, ident, with_for_update=True, populate_existing=True)


async def require_job_admission(db, run, job):
    from backend.agent.orm import HostCreditLedger, HostNodeLease
    from backend.infrastructure.orm import Document
    from backend.agent.util import now
    await require_admission(db, run)
    ledger = await db.scalar(select(HostCreditLedger).where(HostCreditLedger.run_id == run.id,
        HostCreditLedger.job_id == job.id).with_for_update())
    if (run.status not in {"queued", "running"} or ledger is None or ledger.state != "reserved"
            or ledger.details.get("admittedJobHash") != digest({"params": job.params,
                "provider": job.provider_snapshot, "kind": job.kind})):
        raise HostError("admission_required", "排队任务的执行准入已失效", status=409)

    doc = await db.get(Document, uuid.UUID(run.snapshot["id"]))
    node_id = job.params.get("clientRef")
    lease = await db.get(HostNodeLease, (doc.id, node_id)) if doc else None
    node = doc.data.get("objects", {}).get(node_id) if doc else None
    if (node is None or lease is None or lease.run_id != run.id or lease.expires_at <= now()
            or node.get("busyRunId") != str(run.id) or node.get("assetVersion", 0) != lease.expected_version):
        raise HostError("admission_required", "目标画布位置的执行租约已失效", status=409)
    for ident in sorted(set(job.params.get("referenceAssetIds", []))):
        asset = await db.get(Asset, uuid.UUID(ident), with_for_update=True)
        if (asset is None or asset.workspace_id != run.workspace_id or asset.details.get("hostBlocked")
                or asset_identity(asset) != ledger.details.get("admittedInputs", {}).get(ident)):
            raise HostError("asset_forbidden", "生成输入在排队后已变化", status=409)
