"""Planner/reviewer span timestamps; historical unknown times remain NULL.
Revision ID: 0021
Revises: 0020
"""
from alembic import op
import sqlalchemy as sa
revision = "0021"
down_revision = "0020"
branch_labels = depends_on = None


def upgrade():
    for name in ("started_at", "returned_at", "reviewed_at"):
        op.add_column("core_planner_invocations", sa.Column(name, sa.DateTime(timezone=True), nullable=True))


def downgrade():
    for name in ("reviewed_at", "returned_at", "started_at"):
        op.drop_column("core_planner_invocations", name)
