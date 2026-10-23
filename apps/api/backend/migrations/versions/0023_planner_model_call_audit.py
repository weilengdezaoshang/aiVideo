"""Audit columns for one planner model call. Historical rows keep NULL.
Revision ID: 0023
Revises: 0022
"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = depends_on = None


def upgrade():
    op.add_column("core_planner_model_calls", sa.Column("template_hash", sa.String(64), nullable=True))
    op.add_column("core_planner_model_calls", sa.Column("route", sa.String(64), nullable=True))
    op.add_column("core_planner_model_calls", sa.Column("redacted_request", sa.dialects.postgresql.JSONB(), nullable=True))
    op.add_column("core_planner_model_calls", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("core_planner_model_calls", "finished_at")
    op.drop_column("core_planner_model_calls", "redacted_request")
    op.drop_column("core_planner_model_calls", "route")
    op.drop_column("core_planner_model_calls", "template_hash")
