"""Retry plans skip repaired items and do not poison a legal set with a blocked sibling."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.batch.compile import CompileError, compile_rule
from backend.core.batch.planner import proposal_hash
from backend.core.batch.retry import quote_catalog, retry_plan
from tests.backend.test_batch_foreach import _assets, _catalog, _rule


def _outcomes(compiled, *, fail_ordinals, code="provider_down", blocked=(), blind=()):
    rows = {}
    for item in compiled.document["items"]:
        for step in item["steps"]:
            last = step["local_id"] == item["steps"][-1]["local_id"]
            if item["ordinal"] in blocked:
                rows[step["task_key"]] = {"status": "blocked_by_model", "error": {"code": "blocked_by_model"}}
            elif item["ordinal"] in fail_ordinals and last:
                fail_code = "param_invalid" if item["ordinal"] in blind else code
                rows[step["task_key"]] = {"status": "failed", "error": {"code": fail_code}}
            else:
                rows[step["task_key"]] = {
                    "status": "succeeded",
                    "output": {"asset_id": "out-" + step["task_key"], "media_type": "image"},
                    "reusable": True,
                }
    return rows


def test_empty_selection_retries_legal_failures_and_skips_blocked_siblings():
    catalog = _catalog()
    compiled = compile_rule(_rule(3), _assets(3), catalog, catalog.lock,
                            scope={"readable_assets": list(_assets(3))})
    outcomes = _outcomes(compiled, fail_ordinals={0, 1}, blocked={2})
    redo = retry_plan(compiled.document, outcomes, [])
    assert redo["item_count"] == 2
    assert {item["ordinal"] for item in redo["items"]} == {0, 1}


def test_explicit_blocked_item_is_rejected_without_rewriting_others():
    catalog = _catalog()
    compiled = compile_rule(_rule(3), _assets(3), catalog, catalog.lock,
                            scope={"readable_assets": list(_assets(3))})
    outcomes = _outcomes(compiled, fail_ordinals={0}, blocked={1})
    with pytest.raises(CompileError) as denied:
        retry_plan(compiled.document, outcomes, [compiled.document["items"][1]["item_id"]])
    assert denied.value.code == "blocked_by_model"
    legal = compiled.document["items"][0]["item_id"]
    redo = retry_plan(compiled.document, outcomes, [legal])
    assert redo["item_count"] == 1


def test_param_invalid_is_not_retried_unless_explicitly_selected():
    catalog = _catalog()
    compiled = compile_rule(_rule(3), _assets(3), catalog, catalog.lock,
                            scope={"readable_assets": list(_assets(3))})
    outcomes = _outcomes(compiled, fail_ordinals={0, 1}, code="provider_down", blind={0})
    redo = retry_plan(compiled.document, outcomes, [])
    assert redo["item_count"] == 1
    assert redo["items"][0]["ordinal"] == 1
    with pytest.raises(CompileError) as denied:
        retry_plan(compiled.document, outcomes, [compiled.document["items"][0]["item_id"]])
    assert denied.value.code == "not_retryable"


def test_unknown_submitted_cannot_be_treated_as_failure():
    catalog = _catalog()
    compiled = compile_rule(_rule(1), _assets(1), catalog, catalog.lock,
                            scope={"readable_assets": list(_assets(1))})
    outcomes = _outcomes(compiled, fail_ordinals=set())
    last = compiled.document["items"][0]["steps"][-1]["task_key"]
    outcomes[last] = {"status": "submitted", "unknown_submitted": True}
    with pytest.raises(CompileError) as denied:
        retry_plan(compiled.document, outcomes, [])
    assert denied.value.code == "run_not_settled"


def test_queue_timeout_retries_only_when_never_submitted():
    catalog = _catalog()
    compiled = compile_rule(_rule(2), _assets(2), catalog, catalog.lock,
                            scope={"readable_assets": list(_assets(2))})
    outcomes = _outcomes(compiled, fail_ordinals=set())
    a = compiled.document["items"][0]["steps"][-1]["task_key"]
    b = compiled.document["items"][1]["steps"][-1]["task_key"]
    outcomes[a] = {"status": "failed", "error": {"code": "queue_timeout"}, "submitted": False}
    outcomes[b] = {"status": "failed", "error": {"code": "queue_timeout"}, "submitted": True}
    redo = retry_plan(compiled.document, outcomes, [])
    assert redo["item_count"] == 1
    assert redo["items"][0]["ordinal"] == 0


def test_stale_mask_reopens_only_that_items_prefix():
    catalog = _catalog()
    compiled = compile_rule(_rule(2), _assets(2), catalog, catalog.lock,
                            scope={"readable_assets": list(_assets(2))})
    outcomes = _outcomes(compiled, fail_ordinals={0})
    first = compiled.document["items"][0]
    detect = next(step for step in first["steps"] if step["local_id"] == "t1")
    cutout = next(step for step in first["steps"] if step["local_id"] == "t2")
    outcomes[detect["task_key"]] = {
        "status": "succeeded", "output": {"asset_id": "mask-temp", "media_type": "mask"}, "reusable": False}
    outcomes[cutout["task_key"]] = {
        "status": "succeeded", "output": {"asset_id": "cut-1", "media_type": "image"}, "reusable": False}
    redo = retry_plan(compiled.document, outcomes, [first["item_id"]])
    locals_kept = {step["local_id"] for step in redo["items"][0]["steps"]}
    assert locals_kept == {"t1", "t2", "t3"}
    assert redo["item_count"] == 1


def test_catalog_quote_uses_locked_estimate():
    catalog = _catalog()
    compiled = compile_rule(_rule(1), _assets(1), catalog, catalog.lock,
                            scope={"readable_assets": list(_assets(1))})
    outcomes = _outcomes(compiled, fail_ordinals={0})
    redo = retry_plan(compiled.document, outcomes, [])
    assert quote_catalog(redo, catalog) == 1
    bare = SimpleNamespace(lock=SimpleNamespace(version="missing"), get_tool=lambda *_: (_ for _ in ()).throw(ValueError()))
    with pytest.raises(CompileError) as denied:
        quote_catalog(redo, bare)
    assert denied.value.code == "tool_unpriced"


def test_proposal_hash_is_stable():
    first = proposal_hash({"goal": "x", "steps": [{"id": "t1"}]})
    assert first == proposal_hash({"steps": [{"id": "t1"}], "goal": "x"})
    assert first != proposal_hash({"goal": "y", "steps": [{"id": "t1"}]})
