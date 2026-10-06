"""Server-side sessions and explicit workspace membership.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("workspace_members",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("principal_id", sa.String(64), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False))
    op.create_table("browser_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("principal_id", sa.String(64), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_browser_sessions_principal_id", "browser_sessions", ["principal_id"])
    op.create_index("ix_browser_sessions_expires_at", "browser_sessions", ["expires_at"])
    op.create_table("document_requests",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id"), nullable=False))


def downgrade():
    op.drop_table("document_requests")
    op.drop_table("browser_sessions")
    op.drop_table("workspace_members")
