"""Exhaustive legal edges and illegal state pairs, without any infrastructure."""
import itertools

import pytest

from backend.core.contracts import CoreSettings
from backend.core.states import (RUN_EDGES, TASK_EDGES, InvalidTransition, PauseReason,
    RunState, TaskState, validate_transition)

RUN_CASES = [(source, target) for source, targets in RUN_EDGES.items() for target in sorted(targets)]
TASK_CASES = [(source, target) for source, targets in TASK_EDGES.items() for target in sorted(targets)]


@pytest.mark.parametrize("source,target", RUN_CASES)
def test_every_legal_run_edge(source, target):
    validate_transition("run", source, target, reason="waiting_user", resume_state=target)


@pytest.mark.parametrize("source,target", TASK_CASES)
def test_every_legal_task_edge(source, target):
    kind = "wait_user" if target == "waiting_user" or source == "waiting_user" else "call_tool"
    validate_transition("task", source, target, task_kind=kind)


ILLEGAL = [(entity, str(source), str(target)) for entity, enum, edges in
           [("run", RunState, RUN_EDGES), ("task", TaskState, TASK_EDGES)]
           for source, target in itertools.product(enum, repeat=2) if target not in edges.get(source, set())]


@pytest.mark.parametrize("entity,source,target", ILLEGAL)
def test_every_illegal_state_pair(entity, source, target):
    with pytest.raises(InvalidTransition):
        validate_transition(entity, source, target, task_kind="call_tool", reason="waiting_user", resume_state=target)


@pytest.mark.parametrize("reason", list(PauseReason))
def test_pause_reasons_and_matching_resume(reason):
    validate_transition("run", "running", "paused", reason=reason)
    validate_transition("run", "paused", "running", resume_state="running")
    with pytest.raises(InvalidTransition):
        validate_transition("run", "paused", "planning", resume_state="running")


def test_pause_and_wait_user_guards():
    for reason in (None, "unknown"):
        with pytest.raises(InvalidTransition):
            validate_transition("run", "running", "paused", reason=reason)
    with pytest.raises(InvalidTransition):
        validate_transition("task", "validated", "queued", task_kind="wait_user")
    with pytest.raises(InvalidTransition):
        validate_transition("task", "validated", "waiting_user", task_kind="call_tool")
    with pytest.raises(InvalidTransition):
        validate_transition("other", "pending", "validated")


def test_limits_are_configuration(monkeypatch):
    monkeypatch.setenv("AIVERO_CORE_MAX_THOUGHT_ROUNDS", "7")
    monkeypatch.setenv("AIVERO_CORE_TOKENS_LIMIT", "1234")
    settings = CoreSettings()
    assert settings.max_thought_rounds == 7 and settings.tokens_limit == 1234
