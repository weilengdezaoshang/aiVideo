"""Core-domain records; legacy execution tables remain independent adapters."""
import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, String, Text, UniqueConstraint, Uuid, func)
from sqlalchemy.orm import Mapped, mapped_column

from backend.infrastructure.orm import Base, JSONVariant


class Record:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Mutable:
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CoreSession(Record, Mutable, Base):
    __tablename__ = "core_sessions"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    canvas_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("documents.id", ondelete="SET NULL"))
    source_canvas_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[str] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(35))
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    summary_cursor: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    message_cursor: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    recent_assets: Mapped[list] = mapped_column(JSONVariant, default=list, server_default="[]")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("id", "workspace_id"),
        Index("ix_core_session_canvas_user", "workspace_id", "source_canvas_id", "user_id"),
        CheckConstraint("version >= 0 AND summary_cursor >= 0 AND message_cursor >= summary_cursor"))


class CoreSessionMessage(Record, Base):
    __tablename__ = "core_session_messages"
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_sessions.id"))
    sequence: Mapped[int] = mapped_column(BigInteger)
    request_key: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[dict] = mapped_column(JSONVariant)
    processing_status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("session_id", "sequence"), UniqueConstraint("session_id", "request_key"),
        ForeignKeyConstraint(["run_id", "session_id"], ["core_runs.id", "core_runs.session_id"], use_alter=True),
        CheckConstraint("sequence > 0 AND role IN ('user','assistant','system')"),
        CheckConstraint("processing_status IN ('pending','claimed','processed','cancelled')"),
        Index("ix_core_message_pending", "session_id", "processing_status", "sequence"))


class CoreSessionClient(Record, Mutable, Base):
    __tablename__ = "core_session_clients"
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_sessions.id"))
    client_id: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(String(200))
    last_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict] = mapped_column(JSONVariant, default=dict, server_default="{}")
    __table_args__ = (UniqueConstraint("session_id", "client_id"), Index("ix_core_client_lease", "lease_until"))


class CoreRun(Record, Mutable, Base):
    __tablename__ = "core_runs"
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    user_id: Mapped[str] = mapped_column(String(200))
    request_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONVariant)
    mode: Mapped[str] = mapped_column(String(8))
    scope: Mapped[dict] = mapped_column(JSONVariant)
    invariants: Mapped[dict] = mapped_column(JSONVariant)
    version_lock: Mapped[dict] = mapped_column(JSONVariant)
    status: Mapped[str] = mapped_column(String(24), default="created", server_default="created")
    pause_reason: Mapped[str | None] = mapped_column(String(24))
    resume_state: Mapped[str | None] = mapped_column(String(24))
    error: Mapped[dict | None] = mapped_column(JSONVariant)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    credits_limit: Mapped[int] = mapped_column(BigInteger)
    credits_reserved: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    credits_allocated: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    credits_spent: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    tokens_limit: Mapped[int] = mapped_column(BigInteger)
    tokens_reserved: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    tokens_used: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    max_thought_rounds: Mapped[int] = mapped_column(Integer)
    thought_rounds_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_epoch: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    event_cursor: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    current_plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    current_stage: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("id", "session_id"), UniqueConstraint("id", "workspace_id"),
        UniqueConstraint("session_id", "request_key"),
        ForeignKeyConstraint(["session_id", "workspace_id"], ["core_sessions.id", "core_sessions.workspace_id"]),
        ForeignKeyConstraint(["parent_run_id", "session_id"], ["core_runs.id", "core_runs.session_id"]),
        ForeignKeyConstraint(["parent_task_id", "parent_run_id"], ["core_tasks.id", "core_tasks.run_id"], use_alter=True),
        ForeignKeyConstraint(["current_plan_id", "id"], ["core_plans.id", "core_plans.run_id"], use_alter=True),
        CheckConstraint("mode IN ('auto','plan') AND version >= 0 AND execution_epoch >= 0"),
        CheckConstraint("status IN ('created','planning','awaiting_confirm','running','paused','completed','failed','cancelled')"),
        CheckConstraint("parent_run_id IS DISTINCT FROM id AND (parent_task_id IS NULL OR parent_run_id IS NOT NULL)"),
        CheckConstraint("(status = 'paused' AND pause_reason IN ('waiting_user','over_budget','user_paused','waiting_info') AND resume_state IN ('planning','awaiting_confirm','running')) OR (status <> 'paused' AND pause_reason IS NULL AND resume_state IS NULL)"),
        CheckConstraint("0 <= credits_allocated AND 0 <= credits_spent AND credits_allocated + credits_spent <= credits_reserved AND credits_reserved <= credits_limit"),
        CheckConstraint("0 <= tokens_used AND 0 <= tokens_reserved AND tokens_used + tokens_reserved <= tokens_limit"),
        CheckConstraint("0 <= thought_rounds_used AND thought_rounds_used <= max_thought_rounds AND max_thought_rounds > 0"),
        Index("ix_core_run_parent", "parent_run_id"), Index("ix_core_run_session", "session_id"),
        Index("ix_core_run_heartbeat", "status", "heartbeat_at"), Index("ix_core_run_status_updated", "status", "updated_at"),
        Index("ix_core_run_scope", "scope", postgresql_using="gin"),
        Index("ix_core_run_budget", "parent_run_id", "credits_reserved", "credits_allocated", "credits_spent"),
        Index("ix_core_run_token_budget", "status", "tokens_limit", "tokens_used"),
        Index("ix_core_run_thought_budget", "status", "thought_rounds_used", "max_thought_rounds"),
        Index("ix_core_run_invariants", "invariants", postgresql_using="gin"),
        Index("ix_core_run_version_lock", "version_lock", postgresql_using="gin"))


class CorePlan(Record, Base):
    __tablename__ = "core_plans"
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_runs.id"))
    version: Mapped[int] = mapped_column(Integer)
    writer: Mapped[str] = mapped_column(String(16))
    writer_id: Mapped[str | None] = mapped_column(String(200))
    model_call_ref: Mapped[str | None] = mapped_column(String(128))
    raw_output: Mapped[dict | None] = mapped_column(JSONVariant)
    proposal: Mapped[dict] = mapped_column(JSONVariant)
    content_hash: Mapped[str] = mapped_column(String(64))
    rationale: Mapped[dict] = mapped_column(JSONVariant)
    replan_reason: Mapped[str | None] = mapped_column(Text)
    link_constraints: Mapped[dict] = mapped_column(JSONVariant)
    result_type: Mapped[str] = mapped_column(String(16))
    stage: Mapped[str] = mapped_column(String(64))
    milestones: Mapped[list] = mapped_column(JSONVariant)
    self_check: Mapped[dict] = mapped_column(JSONVariant)
    expansion_rules: Mapped[dict] = mapped_column(JSONVariant)
    version_lock: Mapped[dict] = mapped_column(JSONVariant)
    __table_args__ = (UniqueConstraint("run_id", "version"), UniqueConstraint("id", "run_id"),
        UniqueConstraint("id", "run_id", "version"), CheckConstraint("version > 0"),
        CheckConstraint("writer IN ('rule','small','llm','skill','helper','user')"),
        CheckConstraint("result_type IN ('generate','answer_only')"), Index("ix_core_plan_stage", "run_id", "stage"),
        Index("ix_core_plan_milestones", "milestones", postgresql_using="gin"))


class CoreTask(Record, Mutable, Base):
    __tablename__ = "core_tasks"
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_runs.id"))
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    plan_version: Mapped[int] = mapped_column(Integer)
    task_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    tool_ref: Mapped[str | None] = mapped_column(String(128))
    tool_version: Mapped[str | None] = mapped_column(String(128))
    capability_ref: Mapped[str | None] = mapped_column(String(128))
    capability_version: Mapped[str | None] = mapped_column(String(128))
    skill_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("core_skill_versions.id"))
    parameters: Mapped[dict] = mapped_column(JSONVariant)
    output_refs: Mapped[list] = mapped_column(JSONVariant)
    scope: Mapped[dict] = mapped_column(JSONVariant)
    invariants: Mapped[dict] = mapped_column(JSONVariant)
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending")
    review_ref: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer)
    tier: Mapped[str] = mapped_column(String(8))
    credits_reserved: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    actual_credits: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    tokens_limit: Mapped[int] = mapped_column(BigInteger)
    tokens_used: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    output: Mapped[dict | None] = mapped_column(JSONVariant)
    error: Mapped[dict | None] = mapped_column(JSONVariant)
    waiting: Mapped[dict | None] = mapped_column(JSONVariant)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("jobs.id"))
    stage_key: Mapped[str | None] = mapped_column(String(64))
    milestone_key: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_epoch: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("id", "run_id"), UniqueConstraint("id", "run_id", "plan_id"), UniqueConstraint("run_id", "plan_id", "task_key"),
        UniqueConstraint("run_id", "idempotency_key"),
        ForeignKeyConstraint(["plan_id", "run_id", "plan_version"], ["core_plans.id", "core_plans.run_id", "core_plans.version"]),
        CheckConstraint("kind IN ('call_tool','spawn_helper','ask_user','wait_user')"),
        CheckConstraint("status IN ('pending','validated','rejected','queued','submitted','waiting_user','succeeded','failed','blocked_by_model','cancelled')"),
        CheckConstraint("status <> 'waiting_user' OR kind = 'wait_user'"),
        CheckConstraint("kind <> 'wait_user' OR status NOT IN ('queued','submitted')"),
        CheckConstraint("tier IN ('draft','final') AND version >= 0 AND execution_epoch >= 0"),
        CheckConstraint("0 <= retry_count AND retry_count <= max_retries AND 0 <= actual_credits AND actual_credits <= credits_reserved"),
        CheckConstraint("0 <= tokens_used AND tokens_used <= tokens_limit"),
        Index("ix_core_task_scope", "scope", postgresql_using="gin"),
        Index("ix_core_task_due", "run_id", "status"), Index("ix_core_task_heartbeat", "status", "heartbeat_at"))


class CoreTaskDependency(Base):
    __tablename__ = "core_task_dependencies"
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    depends_on_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    __table_args__ = (ForeignKeyConstraint(["task_id", "run_id", "plan_id"], ["core_tasks.id", "core_tasks.run_id", "core_tasks.plan_id"]),
        ForeignKeyConstraint(["depends_on_id", "run_id", "plan_id"], ["core_tasks.id", "core_tasks.run_id", "core_tasks.plan_id"]),
        ForeignKeyConstraint(["plan_id", "run_id"], ["core_plans.id", "core_plans.run_id"]),
        CheckConstraint("task_id <> depends_on_id"))


class CoreTaskAttempt(Record, Base):
    __tablename__ = "core_task_attempts"
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    attempt: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    provider_ref: Mapped[str | None] = mapped_column(String(128))
    provider_task_id: Mapped[str | None] = mapped_column(String(256))
    submission_status: Mapped[str] = mapped_column(String(16))
    submission_certainty: Mapped[str] = mapped_column(String(16))
    registration: Mapped[dict] = mapped_column(JSONVariant)
    result: Mapped[dict | None] = mapped_column(JSONVariant)
    error: Mapped[dict | None] = mapped_column(JSONVariant)
    actual_credits: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (ForeignKeyConstraint(["task_id", "run_id"], ["core_tasks.id", "core_tasks.run_id"]),
        UniqueConstraint("task_id", "attempt"), CheckConstraint("attempt > 0 AND actual_credits >= 0"),
        CheckConstraint("submission_status IN ('registered','sending','submitted','finished')"),
        CheckConstraint("submission_certainty IN ('not_sent','accepted','rejected','unknown')"),
        Index("ix_core_attempt_provider", "provider_ref", "provider_task_id"))


class CoreSkill(Record, Mutable, Base):
    __tablename__ = "core_skills"
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    user_id: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    __table_args__ = (ForeignKeyConstraint(["current_version_id", "id"], ["core_skill_versions.id", "core_skill_versions.skill_id"], use_alter=True),)


class CoreSkillVersion(Record, Base):
    __tablename__ = "core_skill_versions"
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_skills.id"))
    version: Mapped[int] = mapped_column(Integer)
    scenario: Mapped[str] = mapped_column(Text)
    tool_allowlist: Mapped[list] = mapped_column(JSONVariant)
    phases: Mapped[list] = mapped_column(JSONVariant)
    confirmation_points: Mapped[list] = mapped_column(JSONVariant)
    boundaries: Mapped[dict] = mapped_column(JSONVariant)
    source: Mapped[str] = mapped_column(String(16))
    source_ref: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("skill_id", "version"), UniqueConstraint("id", "skill_id"),
        CheckConstraint("version > 0 AND source IN ('official','user')"))


class CoreAsset(Record, Mutable, Base):
    __tablename__ = "core_assets"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("workspaces.id"))
    user_id: Mapped[str] = mapped_column(String(200))
    ownership: Mapped[str] = mapped_column(String(8))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    current_version_number: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    __table_args__ = (UniqueConstraint("id", "workspace_id"),
        ForeignKeyConstraint(["current_version_id", "id", "current_version_number"],
            ["core_asset_versions.id", "core_asset_versions.asset_id", "core_asset_versions.version"], use_alter=True),
        CheckConstraint("ownership IN ('personal','team') AND version >= 0"),
        Index("ix_core_asset_owner", "workspace_id", "user_id", "ownership"))


class CoreAssetVersion(Record, Base):
    __tablename__ = "core_asset_versions"
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_assets.id"))
    version: Mapped[int] = mapped_column(Integer)
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(String(8))
    category: Mapped[str] = mapped_column(String(16))
    card_kind: Mapped[str | None] = mapped_column(String(16))
    starred: Mapped[bool] = mapped_column(default=False, server_default="false")
    recognition: Mapped[dict] = mapped_column(JSONVariant)
    layers: Mapped[dict] = mapped_column(JSONVariant)
    metadata_json: Mapped[dict] = mapped_column(JSONVariant)
    storage: Mapped[dict] = mapped_column(JSONVariant)
    stored_asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("assets.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("core_runs.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    user_id: Mapped[str] = mapped_column(String(200))
    __table_args__ = (ForeignKeyConstraint(["asset_id", "workspace_id"], ["core_assets.id", "core_assets.workspace_id"]),
        ForeignKeyConstraint(["run_id", "workspace_id"], ["core_runs.id", "core_runs.workspace_id"]),
        UniqueConstraint("asset_id", "version"), UniqueConstraint("id", "asset_id"),
        UniqueConstraint("id", "asset_id", "version"),
        ForeignKeyConstraint(["previous_version_id", "asset_id"], ["core_asset_versions.id", "core_asset_versions.asset_id"]),
        ForeignKeyConstraint(["task_id", "run_id"], ["core_tasks.id", "core_tasks.run_id"]),
        CheckConstraint("(version = 1 AND previous_version_id IS NULL) OR (version > 1 AND previous_version_id IS NOT NULL)"),
        CheckConstraint("(size_bytes IS NULL OR size_bytes >= 0) AND (width IS NULL OR width >= 0) AND (height IS NULL OR height >= 0) AND (duration_ms IS NULL OR duration_ms >= 0)"),
        CheckConstraint("version > 0 AND kind IN ('image','video','audio','mask','text')"),
        CheckConstraint("category IN ('artifact','card') AND status IN ('normal','reported','removed')"),
        CheckConstraint("(category = 'artifact' AND card_kind IS NULL) OR (category = 'card' AND card_kind IN ('subject','style','character','environment'))"),
        CheckConstraint("task_id IS NULL OR run_id IS NOT NULL"),
        Index("ix_core_asset_version_state", "workspace_id", "kind", "status"), Index("ix_core_asset_origin", "run_id", "task_id", "user_id"))


class CoreAssetLineage(Record, Base):
    __tablename__ = "core_asset_lineage"
    input_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_asset_versions.id"))
    output_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_asset_versions.id"))
    relation: Mapped[str] = mapped_column(String(32))
    __table_args__ = (UniqueConstraint("input_version_id", "output_version_id", "relation"),
        CheckConstraint("input_version_id <> output_version_id"), Index("ix_core_lineage_output", "output_version_id"))


class CoreEvent(Record, Base):
    __tablename__ = "core_event_outbox"
    aggregate_type: Mapped[str] = mapped_column(String(8))
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    aggregate_version: Mapped[int] = mapped_column(BigInteger)
    run_sequence: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("core_runs.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    plan_version: Mapped[int | None] = mapped_column(Integer)
    causation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("core_event_outbox.id"))
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    from_state: Mapped[str | None] = mapped_column(String(24))
    to_state: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONVariant)
    delivery_status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("aggregate_type", "aggregate_id", "aggregate_version"), UniqueConstraint("run_id", "run_sequence"),
        ForeignKeyConstraint(["task_id", "run_id"], ["core_tasks.id", "core_tasks.run_id"]),
        ForeignKeyConstraint(["plan_id", "run_id", "plan_version"], ["core_plans.id", "core_plans.run_id", "core_plans.version"]),
        CheckConstraint("aggregate_type IN ('run','task') AND aggregate_version >= 0 AND attempts >= 0"),
        CheckConstraint("delivery_status IN ('pending','claimed','published','failed')"),
        CheckConstraint("(plan_id IS NULL) = (plan_version IS NULL)"),
        Index("ix_core_event_run", "run_id", "created_at", "id"), Index("ix_core_event_task", "task_id"),
        Index("ix_core_event_due", "delivery_status", "next_attempt_at", "lease_until"),
        Index("ix_core_event_cause", "causation_id"))
