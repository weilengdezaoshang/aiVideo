"""Document lock precedes Session/Run locks. Node leases and asset CAS share the DAG transaction."""
from __future__ import annotations

import copy
from datetime import timedelta

from backend.agent.orm import HostNodeLease
from backend.agent.util import now
from backend.infrastructure.orm import AssetReference
from .catalog import HostError


async def allocate(db, doc, run, steps, plan, catalog):
    data = copy.deepcopy(doc.data)
    objects, order = data.setdefault("objects", {}), data.setdefault("order", [])
    anchor_x = max((float(obj.get("x", 0)) + float(obj.get("width", 0)) for obj in objects.values()), default=0) + 80
    by_key = {step.key: step for step in steps}
    slot = 0
    for item in plan.steps:
        cap = catalog.resolve(item.capabilityId, item.tier)
        spec = cap.tiers[item.tier]
        for node_id in item.targetNodes:
            lease = await db.get(HostNodeLease, (doc.id, node_id), with_for_update=True)
            if lease and lease.run_id != run.id:
                raise HostError("node_busy", "该位置正在生成中")
            node = objects.get(node_id)
            if node is not None:
                if node.get("busyRunId") and node["busyRunId"] != str(run.id):
                    raise HostError("node_busy", "该位置正在生成中")
                expected = int(node.get("assetVersion", 0))
                node["busyRunId"] = str(run.id)
                node["assetVersion"] = expected
                if node.get("kind") == "error":
                    node["kind"] = "placeholder"
                    node.pop("errorDetail", None)
            else:
                expected = 0
                node = {"id": node_id, "kind": "placeholder", "mediaType": cap.outputType,
                    "x": anchor_x + (slot % 4) * 320, "y": (slot // 4) * 360,
                    "width": 280, "height": round(280 * spec.height / spec.width),
                    "assetVersion": 0, "busyRunId": str(run.id), "createdByRunId": str(run.id),
                    "createdByStepId": item.id, "badge": item.tier}
                objects[node_id] = node
                order.append(node_id)
            if lease is None:
                db.add(HostNodeLease(document_id=doc.id, node_id=node_id, run_id=run.id,
                    step_id=by_key[item.id].id, expected_version=expected,
                    expires_at=now() + timedelta(seconds=120)))
            slot += 1
    doc.data, doc.revision, doc.updated_at = data, doc.revision + 1, now()
    await db.flush()


async def write_asset(db, doc, run, node_id, asset, *, quality_issue=None):
    lease = await db.get(HostNodeLease, (doc.id, node_id), with_for_update=True)
    node = doc.data.get("objects", {}).get(node_id)
    if node is None:
        raise HostError("node_missing", "目标节点已删除")
    if lease is None or lease.run_id != run.id or lease.expires_at <= now():
        raise HostError("lease_lost", "目标节点的生成租约已失效")
    if (node.get("busyRunId") != str(run.id) or
            int(node.get("assetVersion", 0)) != lease.expected_version):
        raise HostError("node_version_conflict", "目标内容已被更新，未覆盖新内容")
    if asset.workspace_id != run.workspace_id:
        raise HostError("asset_forbidden", "生成资产不属于当前工作区")
    data = copy.deepcopy(doc.data)
    target = data["objects"][node_id]
    target.update(kind=asset.kind, assetId=str(asset.id), ext=asset.ext,
        src=f"/api/assets/{asset.id}/content", assetVersion=lease.expected_version + 1,
        badge="review_soft_fail" if quality_issue else run.execution["tier"])
    if quality_issue:
        target["qualityIssue"] = quality_issue
        asset.details = {**asset.details, "hostQualityIssue": quality_issue}
    target.pop("busyRunId", None)
    db.add(AssetReference(asset_id=asset.id, owner_type="document", owner_id=str(doc.id)))
    db.add(AssetReference(asset_id=asset.id, owner_type="agent_run", owner_id=str(run.id)))
    await db.delete(lease)
    doc.data, doc.revision, doc.updated_at = data, doc.revision + 1, now()


async def release(db, doc, run, node_id, code):
    lease = await db.get(HostNodeLease, (doc.id, node_id), with_for_update=True)
    if lease is None or lease.run_id != run.id:
        return
    data = copy.deepcopy(doc.data)
    node = data.get("objects", {}).get(node_id)
    if node and node.get("busyRunId") == str(run.id):
        node.pop("busyRunId", None)
        node.update(kind="error", errorDetail=code)
        doc.data, doc.revision, doc.updated_at = data, doc.revision + 1, now()
    await db.delete(lease)
