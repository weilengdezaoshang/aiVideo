"""SQLAlchemy 2.x ORM 模型(补充要求 §三/原任务 §十一)。

边界:ORM 模型属于基础设施,不作为公开 API 响应直接序列化(§十二.6);
领域/响应模型由应用层 Pydantic Schema 承担。

可移植性:Uuid/JSON 均采用 SQLAlchemy 通用类型(JSON 在 PG 落 JSONB 变体),
保证 Alembic 迁移与本地测试一致性。
"""

import uuid
from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# PG 落 JSONB,本地 SQLite 测试落 JSON,保证迁移与测试环境一致。
JSONVariant = JSONB().with_variant(JSON(), "sqlite")


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid4()


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), default="默认工作区")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="member")


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(64), index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(128))
    details: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MediaAlias(Base):
    __tablename__ = "media_aliases"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"), primary_key=True)
    path: Mapped[str] = mapped_column(String(255), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id"))


class DocumentRequest(Base):
    __tablename__ = "document_requests"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("documents.id"))


class RequestTombstone(Base):
    __tablename__ = "request_tombstones"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(100), default="未命名画布")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (Index("ix_documents_workspace_updated", "workspace_id", "updated_at"),)


class GenerationRequest(Base):
    """幂等受理记录:同 workspace 作用域内 request_id 唯一;不同参数返回冲突。"""

    __tablename__ = "generation_requests"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    request_id: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("jobs.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        Index(
            "uq_generation_requests_scope_key",
            "workspace_id",
            "request_id",
            unique=True,
        ),
    )


class Job(Base):
    """业务状态唯一依据(§六):Celery task 状态不写入本表。"""

    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("jobs.id"), nullable=True, index=True)
    generation_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    params: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str] = mapped_column(String(255), default="排队中")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recovery: Mapped[str | None] = mapped_column(String(16), nullable=True)
    external_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    execution_epoch: Mapped[int] = mapped_column(Integer, default=0)
    phase: Mapped[str] = mapped_column(String(32), default="prepare", server_default="prepare")
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatched_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconcile_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    queue_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    download_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    phase_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    provider_snapshot: Mapped[dict] = mapped_column(JSONVariant, default=dict, server_default="{}")
    phase_payload: Mapped[dict] = mapped_column(JSONVariant, default=dict, server_default="{}")
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 模型执行名额(§八.2):与 Worker 执行槽位不同;领取/释放按 job 自身为所有者。
    slot_scope: Mapped[str | None] = mapped_column(String(128), nullable=True)
    slot_acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index("ix_jobs_workspace_status_created", "workspace_id", "status", "created_at"),
        Index("ix_jobs_status_next_poll", "status", "next_poll_at"),
        Index("ix_jobs_recovery_lease", "status", "lease_until"),
    )


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("jobs.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(32))
    provider_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index("uq_attempts_job_no", "job_id", "attempt_no", unique=True),)


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    kind: Mapped[str] = mapped_column(String(8))
    ext: Mapped[str] = mapped_column(String(8))
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    storage_key: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSONVariant, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (Index("ix_assets_workspace_created", "workspace_id", "created_at"),)


class AssetReference(Base):
    """引用登记:历史裁剪/删除前必须检查,被引用资产不可删除(已知问题 #1 的持久化形态)。"""

    __tablename__ = "asset_references"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id"), index=True)
    owner_type: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        Index("uq_asset_references", "asset_id", "owner_type", "owner_id", unique=True),
    )


class Outbox(Base):
    """事务 Outbox:与业务写入同事务提交;发布确认前不得标记成功(§五.5)。"""

    __tablename__ = "outbox"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    aggregate_type: Mapped[str] = mapped_column(String(16))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(32))
    schema_version: Mapped[str] = mapped_column(String(8), default="1")
    payload: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (Index("ix_outbox_status_next_attempt", "status", "next_attempt_at"),)
