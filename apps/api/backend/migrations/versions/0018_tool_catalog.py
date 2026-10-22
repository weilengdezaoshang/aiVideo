"""Immutable, versioned tool definitions.
Revision ID: 0018
Revises: 0017
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("tool_catalog_versions",
        sa.Column("version", sa.String(128), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("published_by", sa.String(200), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("schema_version = 1", name="ck_tool_catalog_schema"))
    op.create_table("tool_definitions",
        sa.Column("catalog_version", sa.String(128), sa.ForeignKey("tool_catalog_versions.version"), primary_key=True),
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("tool_version", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False))
    op.execute("""CREATE FUNCTION tool_catalog_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'Published tool catalogs are immutable' USING ERRCODE='23000'; END $$""")
    for table in ("tool_catalog_versions", "tool_definitions"):
        op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION tool_catalog_immutable()")
        op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION tool_catalog_immutable()")
    # An index row can only mirror a definition in the sealed snapshot.
    op.execute("""CREATE FUNCTION tool_definition_matches_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF NOT EXISTS (SELECT 1 FROM tool_catalog_versions c,
        jsonb_array_elements(c.snapshot->'tools') t
        WHERE c.version=NEW.catalog_version AND t->>'name'=NEW.name
          AND t->>'version'=NEW.tool_version AND t=NEW.definition) THEN
        RAISE EXCEPTION 'Tool definition differs from sealed snapshot' USING ERRCODE='23000';
      END IF; RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER tool_definition_snapshot BEFORE INSERT ON tool_definitions
      FOR EACH ROW EXECUTE FUNCTION tool_definition_matches_snapshot()""")


def downgrade():
    op.drop_table("tool_definitions")
    op.drop_table("tool_catalog_versions")
    op.execute("DROP FUNCTION tool_definition_matches_snapshot()")
    op.execute("DROP FUNCTION tool_catalog_immutable()")
