"""Agent persona frozen on the Run and a context manifest per model call.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_runs", sa.Column("persona", postgresql.JSONB()))
    op.add_column("agent_model_calls", sa.Column("manifest", postgresql.JSONB()))


def downgrade():
    op.drop_column("agent_model_calls", "manifest")
    op.drop_column("agent_runs", "persona")
