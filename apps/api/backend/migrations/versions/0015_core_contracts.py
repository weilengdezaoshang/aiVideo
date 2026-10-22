"""Versioned core Agent contracts and atomic state/outbox transitions.

Revision ID: 0015
Revises: 0014

DDL is frozen in this migration, independent of future ORM changes.
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# Reviewed, explicit PostgreSQL schema; no runtime model imports.
TABLE_DDL = [
    """CREATE TABLE core_assets (
	workspace_id UUID NOT NULL,
	user_id VARCHAR(200) NOT NULL,
	ownership VARCHAR(8) NOT NULL,
	current_version_id UUID,
	current_version_number INTEGER DEFAULT '0' NOT NULL,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	version INTEGER DEFAULT '0' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (id, workspace_id),
	CHECK (ownership IN ('personal','team') AND version >= 0),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
)""",
    """CREATE TABLE core_skills (
	workspace_id UUID,
	user_id VARCHAR(200),
	name VARCHAR(200) NOT NULL,
	current_version_id UUID,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	version INTEGER DEFAULT '0' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
)""",
    """CREATE TABLE core_sessions (
	workspace_id UUID NOT NULL,
	canvas_id UUID,
	source_canvas_id UUID NOT NULL,
	user_id VARCHAR(200) NOT NULL,
	language VARCHAR(35) NOT NULL,
	summary TEXT DEFAULT '' NOT NULL,
	summary_cursor BIGINT DEFAULT '0' NOT NULL,
	message_cursor BIGINT DEFAULT '0' NOT NULL,
	recent_assets JSONB DEFAULT '[]' NOT NULL,
	closed_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	version INTEGER DEFAULT '0' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (id, workspace_id),
	CHECK (version >= 0 AND summary_cursor >= 0 AND message_cursor >= summary_cursor),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id),
	FOREIGN KEY(canvas_id) REFERENCES documents (id) ON DELETE SET NULL
)""",
    """CREATE TABLE core_skill_versions (
	skill_id UUID NOT NULL,
	version INTEGER NOT NULL,
	scenario TEXT NOT NULL,
	tool_allowlist JSONB NOT NULL,
	phases JSONB NOT NULL,
	confirmation_points JSONB NOT NULL,
	boundaries JSONB NOT NULL,
	source VARCHAR(16) NOT NULL,
	source_ref TEXT,
	content_hash VARCHAR(64) NOT NULL,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (skill_id, version),
	UNIQUE (id, skill_id),
	CHECK (version > 0 AND source IN ('official','user')),
	FOREIGN KEY(skill_id) REFERENCES core_skills (id)
)""",
    """CREATE TABLE core_runs (
	session_id UUID NOT NULL,
	workspace_id UUID NOT NULL,
	parent_run_id UUID,
	parent_task_id UUID,
	user_id VARCHAR(200) NOT NULL,
	request_key VARCHAR(128) NOT NULL,
	request_hash VARCHAR(64) NOT NULL,
	snapshot JSONB NOT NULL,
	mode VARCHAR(8) NOT NULL,
	scope JSONB NOT NULL,
	invariants JSONB NOT NULL,
	version_lock JSONB NOT NULL,
	status VARCHAR(24) DEFAULT 'created' NOT NULL,
	pause_reason VARCHAR(24),
	resume_state VARCHAR(24),
	error JSONB,
	cancel_reason TEXT,
	credits_limit BIGINT NOT NULL,
	credits_reserved BIGINT DEFAULT '0' NOT NULL,
	credits_allocated BIGINT DEFAULT '0' NOT NULL,
	credits_spent BIGINT DEFAULT '0' NOT NULL,
	tokens_limit BIGINT NOT NULL,
	tokens_reserved BIGINT DEFAULT '0' NOT NULL,
	tokens_used BIGINT DEFAULT '0' NOT NULL,
	max_thought_rounds INTEGER NOT NULL,
	thought_rounds_used INTEGER DEFAULT '0' NOT NULL,
	heartbeat_at TIMESTAMP WITH TIME ZONE,
	lease_owner VARCHAR(128),
	lease_until TIMESTAMP WITH TIME ZONE,
	execution_epoch BIGINT DEFAULT '0' NOT NULL,
	event_cursor BIGINT DEFAULT '0' NOT NULL,
	current_plan_id UUID,
	current_stage VARCHAR(64),
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	version INTEGER DEFAULT '0' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (id, session_id),
	UNIQUE (id, workspace_id),
	UNIQUE (session_id, request_key),
	FOREIGN KEY(session_id, workspace_id) REFERENCES core_sessions (id, workspace_id),
	FOREIGN KEY(parent_run_id, session_id) REFERENCES core_runs (id, session_id),
	CHECK (mode IN ('auto','plan') AND version >= 0 AND execution_epoch >= 0),
	CHECK (status IN ('created','planning','awaiting_confirm','running','paused','completed','failed','cancelled')),
	CHECK (parent_run_id IS DISTINCT FROM id AND (parent_task_id IS NULL OR parent_run_id IS NOT NULL)),
	CHECK ((status = 'paused' AND pause_reason IN ('waiting_user','over_budget','user_paused','waiting_info') AND resume_state IN ('planning','awaiting_confirm','running')) OR (status <> 'paused' AND pause_reason IS NULL AND resume_state IS NULL)),
	CHECK (0 <= credits_allocated AND 0 <= credits_spent AND credits_allocated + credits_spent <= credits_reserved AND credits_reserved <= credits_limit),
	CHECK (0 <= tokens_used AND 0 <= tokens_reserved AND tokens_used + tokens_reserved <= tokens_limit),
	CHECK (0 <= thought_rounds_used AND thought_rounds_used <= max_thought_rounds AND max_thought_rounds > 0)
)""",
    """CREATE TABLE core_session_clients (
	session_id UUID NOT NULL,
	client_id VARCHAR(128) NOT NULL,
	user_id VARCHAR(200) NOT NULL,
	last_event_id UUID,
	heartbeat_at TIMESTAMP WITH TIME ZONE NOT NULL,
	lease_until TIMESTAMP WITH TIME ZONE NOT NULL,
	metadata_json JSONB DEFAULT '{}' NOT NULL,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	version INTEGER DEFAULT '0' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (session_id, client_id),
	FOREIGN KEY(session_id) REFERENCES core_sessions (id)
)""",
    """CREATE TABLE core_session_messages (
	session_id UUID NOT NULL,
	sequence BIGINT NOT NULL,
	request_key VARCHAR(128) NOT NULL,
	role VARCHAR(16) NOT NULL,
	content JSONB NOT NULL,
	processing_status VARCHAR(16) DEFAULT 'pending' NOT NULL,
	run_id UUID,
	processed_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (session_id, sequence),
	UNIQUE (session_id, request_key),
	CHECK (sequence > 0 AND role IN ('user','assistant','system')),
	CHECK (processing_status IN ('pending','claimed','processed','cancelled')),
	FOREIGN KEY(session_id) REFERENCES core_sessions (id)
)""",
    """CREATE TABLE core_plans (
	run_id UUID NOT NULL,
	version INTEGER NOT NULL,
	writer VARCHAR(16) NOT NULL,
	writer_id VARCHAR(200),
	model_call_ref VARCHAR(128),
	raw_output JSONB,
	proposal JSONB NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	rationale JSONB NOT NULL,
	replan_reason TEXT,
	link_constraints JSONB NOT NULL,
	result_type VARCHAR(16) NOT NULL,
	stage VARCHAR(64) NOT NULL,
	milestones JSONB NOT NULL,
	self_check JSONB NOT NULL,
	expansion_rules JSONB NOT NULL,
	version_lock JSONB NOT NULL,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (run_id, version),
	UNIQUE (id, run_id),
	UNIQUE (id, run_id, version),
	CHECK (version > 0),
	CHECK (writer IN ('rule','small','llm','skill','helper','user')),
	CHECK (result_type IN ('generate','answer_only')),
	FOREIGN KEY(run_id) REFERENCES core_runs (id)
)""",
    """CREATE TABLE core_tasks (
	run_id UUID NOT NULL,
	plan_id UUID NOT NULL,
	plan_version INTEGER NOT NULL,
	task_key VARCHAR(64) NOT NULL,
	kind VARCHAR(24) NOT NULL,
	tool_ref VARCHAR(128),
	tool_version VARCHAR(128),
	capability_ref VARCHAR(128),
	capability_version VARCHAR(128),
	skill_version_id UUID,
	parameters JSONB NOT NULL,
	output_refs JSONB NOT NULL,
	scope JSONB NOT NULL,
	invariants JSONB NOT NULL,
	status VARCHAR(24) DEFAULT 'pending' NOT NULL,
	review_ref VARCHAR(128),
	idempotency_key VARCHAR(128) NOT NULL,
	retry_count INTEGER DEFAULT '0' NOT NULL,
	max_retries INTEGER NOT NULL,
	tier VARCHAR(8) NOT NULL,
	credits_reserved BIGINT DEFAULT '0' NOT NULL,
	actual_credits BIGINT DEFAULT '0' NOT NULL,
	tokens_limit BIGINT NOT NULL,
	tokens_used BIGINT DEFAULT '0' NOT NULL,
	output JSONB,
	error JSONB,
	waiting JSONB,
	job_id UUID,
	stage_key VARCHAR(64),
	milestone_key VARCHAR(64),
	heartbeat_at TIMESTAMP WITH TIME ZONE,
	lease_owner VARCHAR(128),
	lease_until TIMESTAMP WITH TIME ZONE,
	execution_epoch BIGINT DEFAULT '0' NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	version INTEGER DEFAULT '0' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (id, run_id),
	UNIQUE (id, run_id, plan_id),
	UNIQUE (run_id, plan_id, task_key),
	UNIQUE (run_id, idempotency_key),
	FOREIGN KEY(plan_id, run_id, plan_version) REFERENCES core_plans (id, run_id, version),
	CHECK (kind IN ('call_tool','spawn_helper','ask_user','wait_user')),
	CHECK (status IN ('pending','validated','rejected','queued','submitted','waiting_user','succeeded','failed','blocked_by_model','cancelled')),
	CHECK (status <> 'waiting_user' OR kind = 'wait_user'),
	CHECK (kind <> 'wait_user' OR status NOT IN ('queued','submitted')),
	CHECK (tier IN ('draft','final') AND version >= 0 AND execution_epoch >= 0),
	CHECK (0 <= retry_count AND retry_count <= max_retries AND 0 <= actual_credits AND actual_credits <= credits_reserved),
	CHECK (0 <= tokens_used AND tokens_used <= tokens_limit),
	FOREIGN KEY(run_id) REFERENCES core_runs (id),
	FOREIGN KEY(skill_version_id) REFERENCES core_skill_versions (id),
	FOREIGN KEY(job_id) REFERENCES jobs (id)
)""",
    """CREATE TABLE core_asset_versions (
	workspace_id UUID NOT NULL,
	asset_id UUID NOT NULL,
	version INTEGER NOT NULL,
	previous_version_id UUID,
	kind VARCHAR(8) NOT NULL,
	category VARCHAR(16) NOT NULL,
	card_kind VARCHAR(16),
	starred BOOLEAN DEFAULT 'false' NOT NULL,
	recognition JSONB NOT NULL,
	layers JSONB NOT NULL,
	metadata_json JSONB NOT NULL,
	storage JSONB NOT NULL,
	stored_asset_id UUID,
	content_hash VARCHAR(64) NOT NULL,
	mime_type VARCHAR(128),
	size_bytes BIGINT,
	width INTEGER,
	height INTEGER,
	duration_ms BIGINT,
	status VARCHAR(16) NOT NULL,
	run_id UUID,
	task_id UUID,
	user_id VARCHAR(200) NOT NULL,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(asset_id, workspace_id) REFERENCES core_assets (id, workspace_id),
	FOREIGN KEY(run_id, workspace_id) REFERENCES core_runs (id, workspace_id),
	UNIQUE (asset_id, version),
	UNIQUE (id, asset_id),
	UNIQUE (id, asset_id, version),
	FOREIGN KEY(previous_version_id, asset_id) REFERENCES core_asset_versions (id, asset_id),
	FOREIGN KEY(task_id, run_id) REFERENCES core_tasks (id, run_id),
	CHECK ((version = 1 AND previous_version_id IS NULL) OR (version > 1 AND previous_version_id IS NOT NULL)),
	CHECK ((size_bytes IS NULL OR size_bytes >= 0) AND (width IS NULL OR width >= 0) AND (height IS NULL OR height >= 0) AND (duration_ms IS NULL OR duration_ms >= 0)),
	CHECK (version > 0 AND kind IN ('image','video','audio','mask','text')),
	CHECK (category IN ('artifact','card') AND status IN ('normal','reported','removed')),
	CHECK ((category = 'artifact' AND card_kind IS NULL) OR (category = 'card' AND card_kind IN ('subject','style','character','environment'))),
	CHECK (task_id IS NULL OR run_id IS NOT NULL),
	FOREIGN KEY(asset_id) REFERENCES core_assets (id),
	FOREIGN KEY(stored_asset_id) REFERENCES assets (id),
	FOREIGN KEY(run_id) REFERENCES core_runs (id)
)""",
    """CREATE TABLE core_event_outbox (
	aggregate_type VARCHAR(8) NOT NULL,
	aggregate_id UUID NOT NULL,
	aggregate_version BIGINT NOT NULL,
	run_sequence BIGINT NOT NULL,
	event_type VARCHAR(64) NOT NULL,
	run_id UUID NOT NULL,
	task_id UUID,
	plan_id UUID,
	plan_version INTEGER,
	causation_id UUID,
	correlation_id UUID NOT NULL,
	from_state VARCHAR(24),
	to_state VARCHAR(24) NOT NULL,
	reason TEXT,
	payload JSONB NOT NULL,
	delivery_status VARCHAR(16) DEFAULT 'pending' NOT NULL,
	attempts INTEGER DEFAULT '0' NOT NULL,
	claim_token UUID,
	lease_until TIMESTAMP WITH TIME ZONE,
	next_attempt_at TIMESTAMP WITH TIME ZONE,
	published_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (aggregate_type, aggregate_id, aggregate_version),
	UNIQUE (run_id, run_sequence),
	FOREIGN KEY(task_id, run_id) REFERENCES core_tasks (id, run_id),
	FOREIGN KEY(plan_id, run_id, plan_version) REFERENCES core_plans (id, run_id, version),
	CHECK (aggregate_type IN ('run','task') AND aggregate_version >= 0 AND attempts >= 0),
	CHECK (delivery_status IN ('pending','claimed','published','failed')),
	CHECK ((plan_id IS NULL) = (plan_version IS NULL)),
	FOREIGN KEY(run_id) REFERENCES core_runs (id),
	FOREIGN KEY(causation_id) REFERENCES core_event_outbox (id)
)""",
    """CREATE TABLE core_task_attempts (
	task_id UUID NOT NULL,
	run_id UUID NOT NULL,
	attempt INTEGER NOT NULL,
	idempotency_key VARCHAR(128) NOT NULL,
	provider_ref VARCHAR(128),
	provider_task_id VARCHAR(256),
	submission_status VARCHAR(16) NOT NULL,
	submission_certainty VARCHAR(16) NOT NULL,
	registration JSONB NOT NULL,
	result JSONB,
	error JSONB,
	actual_credits BIGINT DEFAULT '0' NOT NULL,
	submitted_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id, run_id) REFERENCES core_tasks (id, run_id),
	UNIQUE (task_id, attempt),
	CHECK (attempt > 0 AND actual_credits >= 0),
	CHECK (submission_status IN ('registered','sending','submitted','finished')),
	CHECK (submission_certainty IN ('not_sent','accepted','rejected','unknown')),
	UNIQUE (idempotency_key)
)""",
    """CREATE TABLE core_task_dependencies (
	task_id UUID NOT NULL,
	depends_on_id UUID NOT NULL,
	run_id UUID NOT NULL,
	plan_id UUID NOT NULL,
	PRIMARY KEY (task_id, depends_on_id),
	FOREIGN KEY(task_id, run_id, plan_id) REFERENCES core_tasks (id, run_id, plan_id),
	FOREIGN KEY(depends_on_id, run_id, plan_id) REFERENCES core_tasks (id, run_id, plan_id),
	FOREIGN KEY(plan_id, run_id) REFERENCES core_plans (id, run_id),
	CHECK (task_id <> depends_on_id)
)""",
    """CREATE TABLE core_asset_lineage (
	input_version_id UUID NOT NULL,
	output_version_id UUID NOT NULL,
	relation VARCHAR(32) NOT NULL,
	id UUID NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (input_version_id, output_version_id, relation),
	CHECK (input_version_id <> output_version_id),
	FOREIGN KEY(input_version_id) REFERENCES core_asset_versions (id),
	FOREIGN KEY(output_version_id) REFERENCES core_asset_versions (id)
)""",
    """ALTER TABLE core_assets ADD CONSTRAINT fk_core_assets_current_version_id_id_current_version_number FOREIGN KEY(current_version_id, id, current_version_number) REFERENCES core_asset_versions (id, asset_id, version)""",
    """ALTER TABLE core_skills ADD CONSTRAINT fk_core_skills_current_version_id_id FOREIGN KEY(current_version_id, id) REFERENCES core_skill_versions (id, skill_id)""",
    """ALTER TABLE core_runs ADD CONSTRAINT fk_core_runs_current_plan_id_id FOREIGN KEY(current_plan_id, id) REFERENCES core_plans (id, run_id)""",
    """ALTER TABLE core_runs ADD CONSTRAINT fk_core_runs_parent_task_id_parent_run_id FOREIGN KEY(parent_task_id, parent_run_id) REFERENCES core_tasks (id, run_id)""",
    """ALTER TABLE core_session_messages ADD CONSTRAINT fk_core_session_messages_run_id_session_id FOREIGN KEY(run_id, session_id) REFERENCES core_runs (id, session_id)""",
    """CREATE INDEX ix_core_asset_owner ON core_assets (workspace_id, user_id, ownership)""",
    """CREATE INDEX ix_core_session_canvas_user ON core_sessions (workspace_id, source_canvas_id, user_id)""",
    """CREATE INDEX ix_core_run_budget ON core_runs (parent_run_id, credits_reserved, credits_allocated, credits_spent)""",
    """CREATE INDEX ix_core_run_heartbeat ON core_runs (status, heartbeat_at)""",
    """CREATE INDEX ix_core_run_invariants ON core_runs USING gin (invariants)""",
    """CREATE INDEX ix_core_run_parent ON core_runs (parent_run_id)""",
    """CREATE INDEX ix_core_run_scope ON core_runs USING gin (scope)""",
    """CREATE INDEX ix_core_run_session ON core_runs (session_id)""",
    """CREATE INDEX ix_core_run_status_updated ON core_runs (status, updated_at)""",
    """CREATE INDEX ix_core_run_thought_budget ON core_runs (status, thought_rounds_used, max_thought_rounds)""",
    """CREATE INDEX ix_core_run_token_budget ON core_runs (status, tokens_limit, tokens_used)""",
    """CREATE INDEX ix_core_run_version_lock ON core_runs USING gin (version_lock)""",
    """CREATE INDEX ix_core_client_lease ON core_session_clients (lease_until)""",
    """CREATE INDEX ix_core_message_pending ON core_session_messages (session_id, processing_status, sequence)""",
    """CREATE INDEX ix_core_plan_milestones ON core_plans USING gin (milestones)""",
    """CREATE INDEX ix_core_plan_stage ON core_plans (run_id, stage)""",
    """CREATE INDEX ix_core_task_due ON core_tasks (run_id, status)""",
    """CREATE INDEX ix_core_task_heartbeat ON core_tasks (status, heartbeat_at)""",
    """CREATE INDEX ix_core_task_scope ON core_tasks USING gin (scope)""",
    """CREATE INDEX ix_core_asset_origin ON core_asset_versions (run_id, task_id, user_id)""",
    """CREATE INDEX ix_core_asset_version_state ON core_asset_versions (workspace_id, kind, status)""",
    """CREATE INDEX ix_core_event_cause ON core_event_outbox (causation_id)""",
    """CREATE INDEX ix_core_event_due ON core_event_outbox (delivery_status, next_attempt_at, lease_until)""",
    """CREATE INDEX ix_core_event_run ON core_event_outbox (run_id, created_at, id)""",
    """CREATE INDEX ix_core_event_task ON core_event_outbox (task_id)""",
    """CREATE INDEX ix_core_attempt_provider ON core_task_attempts (provider_ref, provider_task_id)""",
    """CREATE INDEX ix_core_lineage_output ON core_asset_lineage (output_version_id)"""
]

FUNCTIONS = [
    """CREATE FUNCTION core_immutable_record() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'immutable core record'; END $$""",
    """CREATE FUNCTION core_guard_state() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 0 OR NEW.status <> (CASE WHEN TG_TABLE_NAME = 'core_runs' THEN 'created' ELSE 'pending' END) THEN
          RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'initial state must be created/pending at version zero';
        END IF;
      ELSE
        IF (NEW.id, NEW.run_id, NEW.plan_id, NEW.plan_version, NEW.kind, NEW.task_key,
            NEW.idempotency_key, NEW.parameters, NEW.scope, NEW.invariants) IS DISTINCT FROM
           (OLD.id, OLD.run_id, OLD.plan_id, OLD.plan_version, OLD.kind, OLD.task_key,
            OLD.idempotency_key, OLD.parameters, OLD.scope, OLD.invariants) THEN
          RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'task identity is immutable';
        END IF;
      END IF;
      IF TG_OP = 'UPDATE' AND pg_trigger_depth() < 2 AND
         (NEW.status, NEW.version, NEW.started_at, NEW.finished_at) IS DISTINCT FROM
         (OLD.status, OLD.version, OLD.started_at, OLD.finished_at) THEN
        RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'use core_transition for all state changes';
      END IF;
      RETURN NEW;
    END $$""",
    """CREATE FUNCTION core_guard_run() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 0 OR NEW.status <> 'created' OR NEW.event_cursor <> 0 THEN
          RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'initial Run state must be created at version zero';
        END IF;
      ELSE
        IF (NEW.id, NEW.session_id, NEW.workspace_id, NEW.parent_run_id, NEW.parent_task_id,
            NEW.user_id, NEW.snapshot, NEW.scope, NEW.invariants, NEW.version_lock) IS DISTINCT FROM
           (OLD.id, OLD.session_id, OLD.workspace_id, OLD.parent_run_id, OLD.parent_task_id,
            OLD.user_id, OLD.snapshot, OLD.scope, OLD.invariants, OLD.version_lock) THEN
          RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'Run identity and frozen context are immutable';
        END IF;
        IF pg_trigger_depth() < 2 AND
           (NEW.status, NEW.version, NEW.pause_reason, NEW.resume_state, NEW.started_at, NEW.finished_at, NEW.event_cursor) IS DISTINCT FROM
           (OLD.status, OLD.version, OLD.pause_reason, OLD.resume_state, OLD.started_at, OLD.finished_at, OLD.event_cursor) THEN
          RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'use core_transition for all Run state changes';
        END IF;
      END IF;
      RETURN NEW;
    END $$""",
    """CREATE FUNCTION core_apply_state_event() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE r core_runs%ROWTYPE; t core_tasks%ROWTYPE; actual_state text;
      expected bigint; valid boolean; changed integer; seq bigint; resume text;
    BEGIN
      IF NEW.aggregate_type = 'run' THEN
        NEW.run_id := NEW.aggregate_id; NEW.task_id := NULL;
      ELSIF NEW.aggregate_type = 'task' THEN
        SELECT * INTO t FROM core_tasks WHERE id = NEW.aggregate_id;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'P4004', MESSAGE = 'task not found'; END IF;
        NEW.run_id := t.run_id; NEW.task_id := t.id;
      ELSE RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'unknown aggregate'; END IF;
      -- Every event for a Run is serialized, including events from separate Tasks.
      SELECT * INTO r FROM core_runs WHERE id = NEW.run_id FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'P4004', MESSAGE = 'run not found'; END IF;
      IF NEW.aggregate_type = 'task' THEN
        SELECT * INTO t FROM core_tasks WHERE id = NEW.aggregate_id;
        actual_state := t.status; NEW.plan_id := t.plan_id; NEW.plan_version := t.plan_version;
      ELSE
        actual_state := r.status; NEW.plan_id := r.current_plan_id;
        SELECT version INTO NEW.plan_version FROM core_plans WHERE id = r.current_plan_id;
      END IF;
      IF NEW.aggregate_version = 0 THEN
        IF pg_trigger_depth() < 2 OR NEW.from_state IS NOT NULL OR
           NEW.to_state <> (CASE WHEN NEW.aggregate_type = 'run' THEN 'created' ELSE 'pending' END) THEN
          RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'creation event belongs to record creation'; END IF;
        NEW.event_type := NEW.aggregate_type || '.created';
      ELSE
        expected := NEW.aggregate_version - 1;
        IF expected <> (CASE WHEN NEW.aggregate_type = 'run' THEN r.version ELSE t.version END) THEN
          RAISE EXCEPTION USING ERRCODE = 'P4001', MESSAGE = 'version_conflict'; END IF;
        IF NEW.from_state IS DISTINCT FROM actual_state THEN
          RAISE EXCEPTION USING ERRCODE = 'P4001', MESSAGE = 'source state changed'; END IF;
        IF NEW.aggregate_type = 'run' THEN
          valid := CASE actual_state
            WHEN 'created' THEN NEW.to_state IN ('planning','failed','cancelled')
            WHEN 'planning' THEN NEW.to_state IN ('awaiting_confirm','running','paused','failed','cancelled')
            WHEN 'awaiting_confirm' THEN NEW.to_state IN ('planning','running','paused','failed','cancelled')
            WHEN 'running' THEN NEW.to_state IN ('planning','paused','completed','failed','cancelled')
            WHEN 'paused' THEN NEW.to_state IN ('failed','cancelled') OR NEW.to_state = r.resume_state
            ELSE false END;
          IF NEW.to_state = 'paused' AND (NEW.reason IS NULL OR NEW.reason NOT IN
              ('waiting_user','over_budget','user_paused','waiting_info')) THEN valid := false; END IF;
          IF NOT COALESCE(valid, false) THEN RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'invalid Run transition'; END IF;
          UPDATE core_runs SET status = NEW.to_state, version = NEW.aggregate_version,
            pause_reason = CASE WHEN NEW.to_state = 'paused' THEN NEW.reason END,
            resume_state = CASE WHEN NEW.to_state = 'paused' THEN actual_state END,
            cancel_reason = CASE WHEN NEW.to_state = 'cancelled' THEN NEW.reason ELSE cancel_reason END,
            execution_epoch = execution_epoch + 1,
            started_at = CASE WHEN NEW.to_state = 'running' THEN COALESCE(started_at, now()) ELSE started_at END,
            finished_at = CASE WHEN NEW.to_state IN ('completed','failed','cancelled') THEN now() END,
            updated_at = now() WHERE id = r.id AND version = expected;
        ELSE
          valid := CASE actual_state
            WHEN 'pending' THEN NEW.to_state IN ('validated','rejected','cancelled')
            WHEN 'validated' THEN NEW.to_state = 'cancelled' OR
              (t.kind = 'wait_user' AND NEW.to_state = 'waiting_user') OR
              (t.kind <> 'wait_user' AND NEW.to_state = 'queued')
            WHEN 'queued' THEN NEW.to_state IN ('submitted','failed','cancelled')
            WHEN 'submitted' THEN NEW.to_state IN ('succeeded','failed','blocked_by_model','cancelled')
            WHEN 'waiting_user' THEN NEW.to_state IN ('succeeded','cancelled')
            ELSE false END;
          IF NOT COALESCE(valid, false) THEN RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'invalid Task transition'; END IF;
          UPDATE core_tasks SET status = NEW.to_state, version = NEW.aggregate_version,
            execution_epoch = execution_epoch + 1,
            started_at = CASE WHEN NEW.to_state IN ('submitted','waiting_user') THEN COALESCE(started_at, now()) ELSE started_at END,
            finished_at = CASE WHEN NEW.to_state IN ('succeeded','failed','blocked_by_model','cancelled','rejected') THEN now() END,
            updated_at = now() WHERE id = t.id AND version = expected;
        END IF;
        GET DIAGNOSTICS changed = ROW_COUNT;
        IF changed <> 1 THEN RAISE EXCEPTION USING ERRCODE = 'P4001', MESSAGE = 'version_conflict'; END IF;
        NEW.event_type := NEW.aggregate_type || '.state_changed';
      END IF;
      UPDATE core_runs SET event_cursor = event_cursor + 1 WHERE id = r.id RETURNING event_cursor INTO seq;
      NEW.run_sequence := seq;
      NEW.correlation_id := COALESCE(NEW.correlation_id, r.id);
      NEW.payload := jsonb_strip_nulls(jsonb_build_object('run_id', NEW.run_id, 'task_id', NEW.task_id, 'plan_id', NEW.plan_id));
      RETURN NEW;
    END $$""",
    """CREATE FUNCTION core_created_event() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      INSERT INTO core_event_outbox (id, aggregate_type, aggregate_id, aggregate_version, to_state)
        VALUES (gen_random_uuid(), CASE WHEN TG_TABLE_NAME = 'core_runs' THEN 'run' ELSE 'task' END,
                NEW.id, 0, NEW.status);
      RETURN NEW;
    END $$""",
    """CREATE FUNCTION core_transition(entity text, ident uuid, expected_version bigint,
        target text, reason_code text DEFAULT NULL, cause uuid DEFAULT NULL, correlation uuid DEFAULT NULL)
        RETURNS uuid LANGUAGE plpgsql AS $$
    DECLARE event_id uuid := gen_random_uuid(); source text;
    BEGIN
      IF entity = 'run' THEN SELECT status INTO source FROM core_runs WHERE id = ident;
      ELSIF entity = 'task' THEN SELECT status INTO source FROM core_tasks WHERE id = ident;
      ELSE RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'unknown entity'; END IF;
      IF source IS NULL THEN RAISE EXCEPTION USING ERRCODE = 'P4004', MESSAGE = 'entity not found'; END IF;
      IF expected_version < 0 OR expected_version IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P4001', MESSAGE = 'version_conflict'; END IF;
      INSERT INTO core_event_outbox (id, aggregate_type, aggregate_id, aggregate_version,
         from_state, to_state, reason, causation_id, correlation_id)
        VALUES (event_id, entity, ident, expected_version + 1, source, target, reason_code, cause, correlation);
      RETURN event_id;
    END $$""",
    """CREATE FUNCTION core_guard_event() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' OR
         (to_jsonb(NEW) - ARRAY['delivery_status','attempts','claim_token','lease_until','next_attempt_at','published_at'])
         IS DISTINCT FROM
         (to_jsonb(OLD) - ARRAY['delivery_status','attempts','claim_token','lease_until','next_attempt_at','published_at']) THEN
        RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'event envelope is immutable'; END IF;
      RETURN NEW;
    END $$""",
    """CREATE FUNCTION core_guard_asset_version() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE a core_assets%ROWTYPE;
    BEGIN
      SELECT * INTO a FROM core_assets WHERE id = NEW.asset_id;
      IF NEW.stored_asset_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM assets WHERE id = NEW.stored_asset_id AND workspace_id = NEW.workspace_id) THEN
        RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'stored asset belongs to another workspace'; END IF;
      IF NEW.version <> a.current_version_number + 1 OR NEW.previous_version_id IS DISTINCT FROM a.current_version_id THEN
        RAISE EXCEPTION USING ERRCODE = 'P4001', MESSAGE = 'asset version conflict'; END IF;
      RETURN NEW;
    END $$""",
    """CREATE FUNCTION core_guard_asset_head() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE v core_asset_versions%ROWTYPE;
    BEGIN
      IF (NEW.id, NEW.workspace_id, NEW.user_id, NEW.ownership) IS DISTINCT FROM
         (OLD.id, OLD.workspace_id, OLD.user_id, OLD.ownership) THEN
        RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'asset identity is immutable'; END IF;
      IF NEW.version <> OLD.version + 1 OR NEW.current_version_number <> OLD.current_version_number + 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'P4001', MESSAGE = 'asset version conflict'; END IF;
      SELECT * INTO v FROM core_asset_versions WHERE id = NEW.current_version_id;
      IF NOT FOUND OR v.asset_id <> NEW.id OR v.previous_version_id IS DISTINCT FROM OLD.current_version_id THEN
        RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'asset head must advance to an appended version'; END IF;
      RETURN NEW;
    END $$""",
    """CREATE FUNCTION core_check_asset_head() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM core_assets WHERE id = NEW.id AND current_version_id IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE = 'P4002', MESSAGE = 'asset requires an initial version'; END IF;
      RETURN NULL;
    END $$""",
]


def upgrade():
    for statement in TABLE_DDL:
        op.execute(statement)
    op.add_column("host_credit_ledger", sa.Column("core_run_id", sa.Uuid(), nullable=True))
    op.add_column("host_credit_ledger", sa.Column("core_task_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_credit_core_run", "host_credit_ledger", "core_runs", ["core_run_id"], ["id"])
    op.create_foreign_key("fk_credit_core_task", "host_credit_ledger", "core_tasks", ["core_task_id", "core_run_id"], ["id", "run_id"])
    op.create_foreign_key("fk_credit_core_workspace", "host_credit_ledger", "core_runs", ["core_run_id", "workspace_id"], ["id", "workspace_id"])
    op.create_check_constraint("ck_credit_core_pair", "host_credit_ledger", "(core_run_id IS NULL) = (core_task_id IS NULL)")
    op.create_index("ix_credit_core_run_task", "host_credit_ledger", ["core_run_id", "core_task_id"])
    for statement in FUNCTIONS:
        op.execute(statement)
    op.execute("CREATE TRIGGER core_state_guard BEFORE INSERT OR UPDATE ON core_runs FOR EACH ROW EXECUTE FUNCTION core_guard_run()")
    op.execute("CREATE TRIGGER core_state_guard BEFORE INSERT OR UPDATE ON core_tasks FOR EACH ROW EXECUTE FUNCTION core_guard_state()")
    for table in ("core_runs", "core_tasks"):
        op.execute(f"CREATE TRIGGER core_created AFTER INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION core_created_event()")
    op.execute("CREATE TRIGGER core_apply_event BEFORE INSERT ON core_event_outbox FOR EACH ROW EXECUTE FUNCTION core_apply_state_event()")
    op.execute("CREATE TRIGGER core_event_guard BEFORE UPDATE OR DELETE ON core_event_outbox FOR EACH ROW EXECUTE FUNCTION core_guard_event()")
    for table in ("core_plans", "core_skill_versions", "core_asset_versions", "core_asset_lineage"):
        op.execute(f"CREATE TRIGGER core_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION core_immutable_record()")
    op.execute("CREATE TRIGGER core_asset_version_guard BEFORE INSERT ON core_asset_versions FOR EACH ROW EXECUTE FUNCTION core_guard_asset_version()")
    op.execute("CREATE TRIGGER core_asset_head_guard BEFORE UPDATE ON core_assets FOR EACH ROW EXECUTE FUNCTION core_guard_asset_head()")
    op.execute("CREATE CONSTRAINT TRIGGER core_asset_head_required AFTER INSERT OR UPDATE ON core_assets DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION core_check_asset_head()")


def downgrade():
    op.drop_constraint("ck_credit_core_pair", "host_credit_ledger")
    op.drop_constraint("fk_credit_core_workspace", "host_credit_ledger")
    op.drop_constraint("fk_credit_core_task", "host_credit_ledger")
    op.drop_constraint("fk_credit_core_run", "host_credit_ledger")
    op.drop_index("ix_credit_core_run_task", table_name="host_credit_ledger")
    op.drop_column("host_credit_ledger", "core_task_id")
    op.drop_column("host_credit_ledger", "core_run_id")
    for table, constraint in (
        ("core_assets", "fk_core_assets_current_version_id_id_current_version_number"),
        ("core_skills", "fk_core_skills_current_version_id_id"),
        ("core_runs", "fk_core_runs_current_plan_id_id"),
        ("core_runs", "fk_core_runs_parent_task_id_parent_run_id"),
        ("core_session_messages", "fk_core_session_messages_run_id_session_id"),
    ):
        op.drop_constraint(constraint, table)
    for table in ("core_event_outbox", "core_asset_lineage", "core_asset_versions", "core_assets",
                  "core_task_attempts", "core_task_dependencies", "core_tasks", "core_skill_versions",
                  "core_skills", "core_plans", "core_runs", "core_session_clients", "core_session_messages", "core_sessions"):
        op.drop_table(table)
    for function in ("core_check_asset_head()", "core_guard_asset_head()", "core_guard_asset_version()",
                     "core_guard_event()", "core_transition(text,uuid,bigint,text,text,uuid,uuid)",
                     "core_created_event()", "core_apply_state_event()", "core_guard_run()",
                     "core_guard_state()", "core_immutable_record()"):
        op.execute(f"DROP FUNCTION {function}")
