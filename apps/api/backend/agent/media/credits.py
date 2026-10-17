"""Workspace credits: reserve the full DAG atomically and settle each provider receipt once."""
from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from backend.agent.orm import HostCreditAccount, HostCreditGrant, HostCreditLedger
from backend.agent.util import encoded, now
from .catalog import HostError


async def account_locked(db, workspace):
    await db.execute(insert(HostCreditAccount).values(workspace_id=workspace, available=0)
                     .on_conflict_do_nothing(index_elements=[HostCreditAccount.workspace_id]))
    return await db.get(HostCreditAccount, workspace, with_for_update=True)


async def grant(db, workspace, request_id, amount, principal):
    if type(amount) is not int or not 1 <= amount <= 1_000_000_000:
        raise HostError("invalid_credit_amount", "积分数量无效", status=400)
    account = await account_locked(db, workspace)
    receipt = await db.get(HostCreditGrant, (workspace, request_id))
    if receipt:
        if receipt.amount != amount:
            raise HostError("idempotency_conflict", "同一积分授予标识不能改变数量")
        return {"available": account.available, "replayed": True}
    db.add(HostCreditGrant(workspace_id=workspace, request_id=request_id, amount=amount, principal=principal))
    account.available += amount
    return {"available": account.available, "replayed": False}


async def reserve(db, run, steps, plan, catalog):
    account = await account_locked(db, run.workspace_id)
    existing = list(await db.scalars(select(HostCreditLedger).where(HostCreditLedger.run_id == run.id)))
    if existing:
        return existing
    quoted = catalog.quote(plan)
    if account.available < quoted:
        raise HostError("quota_exceeded", "额度不足，无法启动本批任务", status=402)
    account.available -= quoted
    ledgers = []
    by_key = {step.key: step for step in steps}
    for item in plan.steps:
        binding = catalog.resolve(item.capabilityId, item.tier)
        for index in range(item.fanOut):
            content = {"workspace": str(run.workspace_id), "document": run.snapshot["id"], "run": str(run.id),
                "step": item.id, "index": index, "tier": item.tier, "capability": item.capabilityId,
                "content": item.model_dump(mode="json"), "sourceAssets": plan.sourceAssetIds}
            key = hashlib.sha256(encoded(content).encode()).hexdigest()
            row = HostCreditLedger(workspace_id=run.workspace_id, run_id=run.id, step_id=by_key[item.id].id,
                child_index=index, attempt=1, idempotency_key=key, quoted=binding.tiers[item.tier].credits,
                charged=0, refunded=0, state="reserved", details={"capabilityId": item.capabilityId,
                    "tier": item.tier, "bindingVersion": binding.binding.version,
                    "adapter": binding.binding.providerSnapshot.get("provider", "ffmpeg"),
                    "modelId": binding.binding.providerSnapshot.get("cloudModel" if binding.outputType == "image" else "videoModel", "optical-flow"),
                    "providerTaskId": None, "startedAt": None, "endedAt": None,
                    "chargePolicy": binding.binding.chargePolicy,
                    "parallelGroup": item.parallelGroup, "targetNode": item.targetNodes[index],
                    "sourceAssetIds": plan.sourceAssetIds, "principal": run.principal})
            db.add(row)
            ledgers.append(row)
    await db.flush()
    return ledgers


async def settle(db, ledger, job=None, *, cancelled=False, error_code=None):
    if ledger.state != "reserved":
        if cancelled and ledger.charged:
            ledger.state = "cancelled_with_charge"
            ledger.details = {**ledger.details, "cancelled": True}
            ledger.updated_at = now()
        return
    submitted = bool(job and (job.external_task_id or job.attempt_count))
    success = bool(job and job.status == "completed")
    charged = ledger.quoted if success or (submitted and ledger.details["chargePolicy"] == "on_submission") else 0
    refunded = ledger.quoted - charged
    account = await account_locked(db, ledger.workspace_id)
    account.available += refunded
    ledger.charged, ledger.refunded, ledger.updated_at = charged, refunded, now()
    ledger.state = ("cancelled_with_charge" if cancelled and charged else "settled" if charged else "refunded")
    ledger.details = {**ledger.details, "providerTaskId": job.external_task_id if job else None,
        "errorCode": error_code or (job.error_code if job else None),
        "jobId": str(job.id) if job else None, "providerStatus": job.status if job else None,
        "cancelled": cancelled, "startedAt": job.created_at.isoformat() if job else None,
        "endedAt": job.finished_at.isoformat() if job and job.finished_at else now().isoformat(),
        "durationMs": max(0, int(((job.finished_at or now()) - job.created_at).total_seconds() * 1000)) if job else 0}
