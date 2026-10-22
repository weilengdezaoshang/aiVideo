"""Durable execution receipts and shared credit-ledger Core ownership.
Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa
revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

# Frozen PostgreSQL DDL; no runtime metadata imports in migrations.
DDL = ['CREATE TABLE core_execution_admissions (\n\tid UUID NOT NULL, \n\ttask_id UUID NOT NULL, \n\tplan_id UUID NOT NULL, \n\treview_ref VARCHAR(128) NOT NULL, \n\ttask_hash VARCHAR(64) NOT NULL, \n\tcatalog_hash VARCHAR(64) NOT NULL, \n\tchecks JSONB NOT NULL, \n\tplacement JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (task_id), \n\tFOREIGN KEY(task_id) REFERENCES core_tasks (id), \n\tFOREIGN KEY(plan_id) REFERENCES core_plans (id)\n)', "CREATE TABLE core_task_executions (\n\ttask_id UUID NOT NULL, \n\tjob_id UUID NOT NULL, \n\tcausation_id UUID NOT NULL, \n\tversion INTEGER NOT NULL, \n\tartifact_epoch INTEGER DEFAULT '0' NOT NULL, \n\tphase VARCHAR(32) NOT NULL, \n\tlease_token UUID, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tnext_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tdeadline TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcancel_requested BOOLEAN NOT NULL, \n\tattempt_id UUID, \n\tprepared JSONB NOT NULL, \n\tresolved_parameters JSONB NOT NULL, \n\tticket TEXT, \n\texpires_at TIMESTAMP WITH TIME ZONE, \n\tartifact JSONB, \n\tasset_id UUID, \n\tresult_credits BIGINT NOT NULL, \n\tretries INTEGER NOT NULL, \n\tPRIMARY KEY (task_id), \n\tCHECK (version >= 0 AND retries >= 0 AND result_credits >= 0), \n\tCHECK (phase IN ('ready','preparing','sending','poll','download','publish','unknown','done')), \n\tFOREIGN KEY(task_id) REFERENCES core_tasks (id), \n\tUNIQUE (job_id), \n\tFOREIGN KEY(job_id) REFERENCES jobs (id), \n\tFOREIGN KEY(causation_id) REFERENCES core_event_outbox (id), \n\tFOREIGN KEY(attempt_id) REFERENCES core_task_attempts (id), \n\tFOREIGN KEY(asset_id) REFERENCES assets (id)\n)", 'CREATE INDEX ix_core_execution_due ON core_task_executions (phase, next_at, lease_until)', 'CREATE TABLE core_provider_receipts (\n\tid UUID NOT NULL, \n\tprovider_ref VARCHAR(128) NOT NULL, \n\tprovider_task_id VARCHAR(256) NOT NULL, \n\ttask_id UUID NOT NULL, \n\tresult_hash VARCHAR(64) NOT NULL, \n\tticket TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (provider_ref, provider_task_id), \n\tFOREIGN KEY(task_id) REFERENCES core_tasks (id)\n)', 'CREATE TABLE core_canvas_writes (\n\ttask_id UUID NOT NULL, \n\tdocument_id UUID NOT NULL, \n\tnode_id VARCHAR(200), \n\tasset_id UUID NOT NULL, \n\tconflict BOOLEAN NOT NULL, \n\thistory_only BOOLEAN NOT NULL, \n\trect JSONB NOT NULL, \n\tPRIMARY KEY (task_id), \n\tFOREIGN KEY(task_id) REFERENCES core_tasks (id), \n\tFOREIGN KEY(document_id) REFERENCES documents (id), \n\tFOREIGN KEY(asset_id) REFERENCES assets (id)\n)']


def upgrade():
    op.alter_column("jobs", "external_task_id", type_=sa.String(256))
    op.alter_column("host_credit_ledger", "run_id", nullable=True)
    op.alter_column("host_credit_ledger", "step_id", nullable=True)
    op.create_check_constraint("ck_credit_owner", "host_credit_ledger",
        "(run_id IS NOT NULL AND step_id IS NOT NULL AND core_run_id IS NULL) OR (run_id IS NULL AND step_id IS NULL AND core_run_id IS NOT NULL)")
    op.create_index("uq_credit_core_task", "host_credit_ledger", ["core_task_id"], unique=True)
    for statement in DDL:
        op.execute(statement)
    op.execute("CREATE TRIGGER core_execution_admission_immutable BEFORE UPDATE OR DELETE ON core_execution_admissions FOR EACH ROW EXECUTE FUNCTION core_immutable_record()")


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM host_credit_ledger WHERE core_run_id IS NOT NULL)
       THEN RAISE EXCEPTION 'Core credit receipts must be reconciled and archived before downgrade'; END IF; END $$""")
    for table in ("core_canvas_writes", "core_provider_receipts", "core_task_executions", "core_execution_admissions"):
        op.drop_table(table)
    op.drop_index("uq_credit_core_task", table_name="host_credit_ledger")
    op.drop_constraint("ck_credit_owner", "host_credit_ledger")
    op.alter_column("host_credit_ledger", "run_id", nullable=False)
    op.alter_column("host_credit_ledger", "step_id", nullable=False)
    op.alter_column("jobs", "external_task_id", type_=sa.String(128))
