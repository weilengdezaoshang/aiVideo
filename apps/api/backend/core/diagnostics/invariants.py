"""Shared read-only invariant checker; never repairs data to make a test pass."""

from sqlalchemy import select, func
from backend.agent.orm import HostCreditLedger, HostCreditAccount, HostCreditGrant
from backend.core.orm import CoreRun, CoreTask, CorePlan, CoreTaskDependency, CoreAssetVersion
from backend.core.execution.orm import CanvasWrite
from backend.infrastructure.orm import Asset, Document
from backend.core.supervisor.service import TERMINAL, TASK_TERMINAL


async def check(tx, run_id, *, settled=True):
    run = await tx.get(CoreRun, run_id)
    errors = []
    if run is None:
        return [{"code": "missing_run", "run_id": str(run_id)}]
    tasks = list(await tx.scalars(select(CoreTask).where(CoreTask.run_id == run.id)))
    task_ids = {t.id for t in tasks}
    for task in tasks:
        plan = await tx.get(CorePlan, task.plan_id)
        if plan is None or plan.run_id != run.id or plan.version != task.plan_version:
            errors.append({"code": "orphan_task", "task_id": str(task.id)})
        if run.status in TERMINAL and task.status not in TASK_TERMINAL:
            errors.append({"code": "terminal_run_live_task", "task_id": str(task.id)})
    for edge in await tx.scalars(
        select(CoreTaskDependency).where(CoreTaskDependency.run_id == run.id)
    ):
        if edge.task_id not in task_ids or edge.depends_on_id not in task_ids:
            errors.append({"code": "orphan_dependency", "task_id": str(edge.task_id)})
    ledgers = list(
        await tx.scalars(select(HostCreditLedger).where(HostCreditLedger.core_run_id == run.id))
    )
    for row in ledgers:
        if row.core_task_id not in task_ids:
            errors.append({"code": "orphan_credit", "ledger_id": str(row.id)})
        remaining = row.quoted - row.charged - row.refunded
        if remaining < 0 or (
            (settled or run.status in TERMINAL) and (remaining != 0 or row.state == "reserved")
        ):
            errors.append(
                {"code": "credit_balance", "ledger_id": str(row.id), "outstanding": remaining}
            )
    if settled and run.credits_reserved != run.credits_spent + run.credits_allocated:
        errors.append({"code": "run_reservation_mismatch"})
    if sum(row.charged for row in ledgers) != run.credits_spent:
        errors.append({"code": "run_spend_mismatch"})
    account = await tx.get(HostCreditAccount, run.workspace_id)
    if account is not None:
        grants = await tx.scalar(
            select(func.coalesce(func.sum(HostCreditGrant.amount), 0)).where(
                HostCreditGrant.workspace_id == run.workspace_id
            )
        )
        consumed = await tx.scalar(
            select(func.coalesce(func.sum(HostCreditLedger.charged), 0)).where(
                HostCreditLedger.workspace_id == run.workspace_id
            )
        )
        held = await tx.scalar(
            select(
                func.coalesce(
                    func.sum(
                        HostCreditLedger.quoted
                        - HostCreditLedger.charged
                        - HostCreditLedger.refunded
                    ),
                    0,
                )
            ).where(HostCreditLedger.workspace_id == run.workspace_id)
        )
        if account.available + consumed + held != grants:
            errors.append({"code": "workspace_credit_balance"})
    elif ledgers:
        errors.append({"code": "missing_credit_account"})
    writes = (
        list(await tx.scalars(select(CanvasWrite).where(CanvasWrite.task_id.in_(task_ids))))
        if task_ids
        else []
    )
    for write in writes:
        asset = await tx.get(Asset, write.asset_id)
        origin = await tx.scalar(
            select(CoreAssetVersion.id).where(
                CoreAssetVersion.stored_asset_id == write.asset_id,
                CoreAssetVersion.run_id == run.id,
                CoreAssetVersion.task_id == write.task_id,
                CoreAssetVersion.user_id == run.user_id,
            )
        )
        if asset is None or origin is None:
            errors.append({"code": "missing_lineage", "task_id": str(write.task_id)})
        if settled and write.node_id and not write.history_only:
            doc = await tx.get(Document, write.document_id)
            node = doc.data.get("objects", {}).get(write.node_id) if doc else None
            if not node or node.get("assetId") != str(write.asset_id):
                errors.append({"code": "canvas_receipt_mismatch", "task_id": str(write.task_id)})
    # Detect generated nodes with no publication receipt, not only missing lineage on known receipts.
    from backend.core.orm import CoreSession

    session = await tx.get(CoreSession, run.session_id)
    doc = await tx.get(Document, session.canvas_id) if session.canvas_id else None
    for ident, node in (doc.data.get("objects", {}) if doc else {}).items():
        if node.get("assetId") and ident.startswith("generated-"):
            task_id = ident.removeprefix("generated-")
            if task_id in {str(t) for t in task_ids} and not any(
                w.node_id == ident for w in writes
            ):
                errors.append({"code": "unreceipted_canvas_artifact", "node_id": ident})
    return errors
