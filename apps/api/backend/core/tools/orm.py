"""Published catalog rows are append-only, including direct SQL writes."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.infrastructure.orm import Base, JSONVariant


class ToolCatalogVersion(Base):
    __tablename__ = "tool_catalog_versions"
    version: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(Integer)
    published_by: Mapped[str] = mapped_column(String(200))
    snapshot: Mapped[dict] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("schema_version = 1"),)


class ToolDefinitionRecord(Base):
    __tablename__ = "tool_definitions"
    catalog_version: Mapped[str] = mapped_column(String(128), ForeignKey("tool_catalog_versions.version"), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    tool_version: Mapped[str] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64))
    definition: Mapped[dict] = mapped_column(JSONVariant)
