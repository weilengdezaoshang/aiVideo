"""Pydantic compatibility projections never expose provider secrets or signed URLs."""
from typing import Literal
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class JobResponse(BaseModel):
    id: str
    status: Literal["queued", "running", "completed", "failed", "unknown"]
    kind: str
    params: dict
    progress: float = 0
    message: str
    images: list[dict] = Field(default_factory=list)
    createdAt: str
    documentId: str | None = None
    clientRef: str | None = None
    requestId: str | None = None
    code: str | None = None
    recovery: str | None = None
    retryAfter: float | None = None
    stateVersion: int
    phase: str
    batchCount: int = 1
    children: list["JobResponse"] = Field(default_factory=list)
    groupId: str | None = None
    slot: int | None = None
    directionTitle: str | None = None


class JobEnvelope(BaseModel):
    job: JobResponse


class JobsResponse(BaseModel):
    jobs: list[JobResponse]


class AcceptedJobResponse(JobEnvelope):
    jobId: str


def project_job(row) -> dict:
    messages = {"queued": "排队中", "running": "正在生成", "unknown": "生成结果待确认，请勿重复提交",
                "completed": "生成完成", "failed": "任务未完成"}
    message = messages[row.status]
    if row.error_code == "QUEUE_TIMEOUT":
        message = "排队超时，尚未提交到供应商"
    elif row.phase in {"download", "persist"}:
        message = "正在恢复产物保存，不会重新生成"
    elif row.phase in {"poll", "reconcile"} and row.error_code:
        message = "查询暂不可用，保留原任务继续确认"
    params = {k: v for k, v in row.params.items() if k not in {"referenceStorage", "maskStorage", "submittedBy", "sources"}}
    images = []
    if row.status == "completed" and row.phase_payload.get("assetId"):
        asset_id = row.phase_payload["assetId"]
        images.append({"id": asset_id, "jobId": str(row.id),
            "file": f"{asset_id}.{row.phase_payload['ext']}", "url": f"/api/assets/{asset_id}/content",
            "params": params, "createdAt": row.finished_at.isoformat(),
            "provider": row.provider_snapshot.get("provider", "unknown")})
    return JobResponse(id=str(row.id), status=row.status, kind=row.kind, params=params,
        progress=row.progress or 0, message=message, images=images,
        createdAt=row.created_at.isoformat(), documentId=str(row.document_id) if row.document_id else None,
        clientRef=params.get("clientRef"), requestId=params.get("requestId"),
        groupId=params.get("groupId"), slot=params.get("slot"), directionTitle=params.get("directionTitle"),
        code=row.error_code, recovery=row.recovery, stateVersion=row.state_version,
        retryAfter=max(0, (row.next_poll_at - datetime.now(timezone.utc)).total_seconds()) if row.next_poll_at else None,
        phase=row.phase).model_dump()
