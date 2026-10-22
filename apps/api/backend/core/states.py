"""Exhaustive state vocabulary and pure validation, mirrored by the frozen migration."""
from enum import StrEnum


class RunState(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    AWAITING_CONFIRM = "awaiting_confirm"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskState(StrEnum):
    PENDING = "pending"
    VALIDATED = "validated"
    REJECTED = "rejected"
    QUEUED = "queued"
    SUBMITTED = "submitted"
    WAITING_USER = "waiting_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED_BY_MODEL = "blocked_by_model"
    CANCELLED = "cancelled"


class TaskKind(StrEnum):
    CALL_TOOL = "call_tool"
    SPAWN_HELPER = "spawn_helper"
    ASK_USER = "ask_user"
    WAIT_USER = "wait_user"


class PauseReason(StrEnum):
    WAITING_USER = "waiting_user"
    OVER_BUDGET = "over_budget"
    USER_PAUSED = "user_paused"
    WAITING_INFO = "waiting_info"


RUN_EDGES = {
    "created": {"planning", "failed", "cancelled"},
    "planning": {"awaiting_confirm", "running", "paused", "failed", "cancelled"},
    "awaiting_confirm": {"planning", "running", "paused", "failed", "cancelled"},
    "running": {"planning", "paused", "completed", "failed", "cancelled"},
    "paused": {"planning", "awaiting_confirm", "running", "failed", "cancelled"},
}
TASK_EDGES = {
    "pending": {"validated", "rejected", "cancelled"},
    "validated": {"queued", "waiting_user", "cancelled"},
    "queued": {"submitted", "failed", "cancelled"},
    "submitted": {"succeeded", "failed", "blocked_by_model", "cancelled"},
    "waiting_user": {"succeeded", "cancelled"},
}


class ContractError(Exception):
    code = "contract_error"


class InvalidTransition(ContractError):
    code = "invalid_transition"


class VersionConflict(ContractError):
    code = "version_conflict"


class EntityNotFound(ContractError):
    code = "entity_not_found"


def validate_transition(entity, source, target, *, task_kind=None, reason=None, resume_state=None):
    edges = RUN_EDGES if entity == "run" else TASK_EDGES if entity == "task" else {}
    if target not in edges.get(source, set()):
        raise InvalidTransition(f"{entity}: {source} -> {target}")
    if entity == "run":
        if target == "paused" and reason not in set(PauseReason):
            raise InvalidTransition("paused requires a valid pause reason")
        if source == "paused" and target not in {"failed", "cancelled", resume_state}:
            raise InvalidTransition("resume target must equal resume_state")
    else:
        if task_kind not in set(TaskKind):
            raise InvalidTransition("unknown task kind")
        if target == "waiting_user" and task_kind not in {"wait_user", "ask_user"}:
            raise InvalidTransition("only interaction tasks can wait for a reply")
        if task_kind in {"wait_user", "ask_user"} and target in {"queued", "submitted"}:
            raise InvalidTransition("wait_user cannot enter the execution queue")
