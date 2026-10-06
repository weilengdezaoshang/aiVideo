"""Agent evidence envelopes: verified claims, repair counters and tool-call kinds.

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_runs", sa.Column("claims", postgresql.JSONB()))
    op.add_column("agent_runs", sa.Column("counters", postgresql.JSONB(), nullable=False,
                                          server_default=sa.text("'{}'::jsonb")))
    op.add_column("agent_tool_calls", sa.Column("kind", sa.String(16), nullable=False, server_default="read"))
    op.add_column("agent_tool_calls", sa.Column("tool_version", sa.Integer()))
    op.add_column("agent_tool_calls", sa.Column("dedupe_of", sa.String(32)))


def downgrade():
    op.drop_column("agent_tool_calls", "dedupe_of")
    op.drop_column("agent_tool_calls", "tool_version")
    op.drop_column("agent_tool_calls", "kind")
    op.drop_column("agent_runs", "counters")
    op.drop_column("agent_runs", "claims")
