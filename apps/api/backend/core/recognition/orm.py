"""Recognition job rows: durable queue for ingest-time image analysis."""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid

from sqlalchemy.orm import Mapped, mapped_column

from backend.core.orm import Record
from backend.infrastructure.orm import Base, JSONVariant


class CoreRecognitionJob(Record, Base):
    __tablename__ = "core_recognition_jobs"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    stored_asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id"))
    requested_by: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    core_asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    core_asset_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    result: Mapped[dict] = mapped_column(JSONVariant, default=dict, server_default="{}")
    error: Mapped[dict | None] = mapped_column(JSONVariant)
    request_key: Mapped[str] = mapped_column(String(128))
    __table_args__ = (
        CheckConstraint("source IN ('upload','generation') AND status IN ('pending','claimed','succeeded','failed','skipped')"),
        CheckConstraint("attempt >= 0"),
        Index("ix_core_recognition_due", "status", "next_at"),
        Index("ix_core_recognition_asset", "stored_asset_id"),
        {"extend_existing": False},
    )
