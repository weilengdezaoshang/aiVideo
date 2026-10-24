"""for_each compiles once, isolates items, and retries only the failed suffix."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.core.batch.compile import CompileError, compile_rule, restore
from backend.core.batch.contracts import BatchSettings
from backend.core.batch.retry import budget_allows, quote_of, retry_plan
from backend.core.batch.schedule import DispatchLedger, claim_decision, item_progress
from backend.core.supervisor.contracts import Plan, PlannerResult, Settings, Step
from backend.core.supervisor.reviewer import review
from backend.core.supervisor.tool_calls import convert_tool_calls
from backend.core.tools.catalog import ToolCatalog
from backend.core.tools.contracts import CatalogSnapshot, Execution, Port, Price, ToolDefinition
from backend.core.tools.definitions import object_schema


def _execution() -> Execution:
    return Execution(adapter="fake", result_url_policy="authenticated_asset", sla_seconds=5)


def _tool(name: str, properties: dict, *, available=True, priced=True, inputs=None, outputs=None):
    price = Price(kind="fixed", credits=1) if priced else Price(kind="unpriced", credits=None)
    return ToolDefinition(
        name=name, version="1", description=name, parameters=object_schema(properties),
        result_schema=object_schema({"ok": {"type": "boolean"}}), inputs=inputs or [],
        outputs=outputs or [Port(path="output", media_type="image")],
        mode="read_only" if name == "detect_subject" else "deterministic", price=price,
        limits={}, execution=_execution(), failures=[], available=available,
        unavailable_reason=None if available else "unbound")


def _image_schema():
    literal = object_schema({"asset_id": {"type": "string"}, "media_type": {"const": "image"}})
    return {"anyOf": [literal, {"type": "string"}]}


def _catalog():
    image = _image_schema()
    image_port = Port(path="image", media_type="image")
    tools = [
        _tool("detect_subject", {"image": image}, inputs=[image_port],
              outputs=[Port(path="output", media_type="mask")]),
        _tool("cutout", {"image": image, "mask": {"type": "string"}},
              inputs=[image_port, Port(path="mask", media_type="mask")]),
        _tool("compose_layers", {"subject": {"type": "string"}},
              inputs=[Port(path="subject", media_type="image", required=False)]),
        _tool("text_layout", {"text": {"type": "string"}}, outputs=[Port(path="output", media_type="text")]),
    ]
    return ToolCatalog(CatalogSnapshot(version="batch-test", tools=tools))


def _template():
    return [
        {"id": "t1", "tool": "detect_subject", "parameters": {"image": {"$item": "asset"}}},
        {"id": "t2", "tool": "cutout", "parameters": {"image": {"$item": "asset"}, "mask": "$t1.output"},
         "depends_on": ["t1"]},
        {"id": "t3", "tool": "compose_layers", "parameters": {"subject": "$t2.output"}, "depends_on": ["t2"]},
    ]


def _assets(count: int):
    return {f"asset-{index}": {"version": 3} for index in range(count)}


def _rule(count: int, template=None, sizes=None):
    body = {"inputs": [{"asset_id": f"asset-{index}"} for index in range(count)],
            "task_template": template or _template()}
    if sizes is not None:
        body["sizes"] = sizes
    return body


class FakePlanner:
    def __init__(self, catalog):
        self.catalog = catalog
        self.calls = 0

    async def plan(self, context):
        self.calls += 1
        return convert_tool_calls(
            [{"id": "foreach", "name": "for_each", "arguments": _rule(100)}],
            context=context, catalog=self.catalog, user={"rollout_group": "beta"},
            version_lock=self.catalog.lock, settings=Settings(planner_tool_threshold=200))


def test_one_planner_call_expands_300_tasks_and_reviews_before_dispatch():
    catalog = _catalog()
    planner = FakePlanner(catalog)
    context = {"request": {"message": "批量处理这些商品"}, "scope": {"readable_assets": list(_assets(100))},
               "snapshot": {"asset_versions": {key: 3 for key in _assets(100)}}}
    result = asyncio.run(planner.plan(context))
    assert planner.calls == 1
    assert isinstance(result, PlannerResult)
    assert len(result.plan.steps) == 300
    assert result.plan.expansion["compiler_version"] == "batch-compile-v1"
    restored = restore(result.plan.expansion)
    assert [step.id for step in restored.steps] == [step.id for step in result.plan.steps]
    run = SimpleNamespace(
        snapshot={"request": {"message": "批量处理这些商品"},
                  "assets": [{"asset_id": f"asset-{index}", "owner_id": "u", "kind": "image"} for index in range(100)]},
        invariants={}, user_id="u",
        credits_limit=1000, credits_spent=0,
        scope={"tools": ["detect_subject", "cutout", "compose_layers"], "readable_assets": list(_assets(100)), "writable_regions": ["board"]},
        version_lock={"tool_catalog": catalog.lock.model_dump()})
    verdict = review(result.plan, run, catalog, 1000)
    assert verdict["verdict"] == "pass"
    assert verdict["quoted_credits"] == 300
    assert "task_limit" not in {item["code"] for item in verdict["reasons"]}


def test_ordinary_plan_still_stops_at_supervisor_max_tasks():
    steps = [Step(id=f"t{index}", kind="ask_user", parameters={"question": "尺寸"}) for index in range(1, 102)]
    plan = Plan(goal="普通计划", steps=steps, result_type="generate", writer="rule")
    catalog = _catalog()
    run = SimpleNamespace(
        snapshot={"request": {"message": "普通计划"}}, invariants={}, user_id="u", credits_limit=10, credits_spent=0,
        scope={"tools": [], "readable_assets": [], "writable_regions": []},
        version_lock={"tool_catalog": catalog.lock.model_dump()})
    verdict = review(plan, run, catalog, 10)
    assert any(item["code"] == "task_limit" for item in verdict["reasons"])


def test_dependencies_do_not_cross_items_and_versions_are_stable():
    catalog = _catalog()
    compiled = compile_rule(_rule(2), _assets(2), catalog, catalog.lock, scope={"readable_assets": ["asset-0", "asset-1"]})
    again = compile_rule(_rule(2), _assets(2), catalog, catalog.lock, scope={"readable_assets": ["asset-0", "asset-1"]})
    assert compiled.document["items"][0]["item_id"] == again.document["items"][0]["item_id"]
    assert compiled.document["items"][0]["asset_version"] == 3
    second = compiled.steps[4]
    assert second.parameters["mask"] == "$t4.output"
    assert all("$t1.output" not in str(step.parameters) for step in compiled.steps[3:])
    with pytest.raises(CompileError) as conflict:
        compile_rule({"inputs": [{"asset_id": "asset-0", "version": 9}], "task_template": _template()},
                     _assets(1), catalog, catalog.lock, scope={"readable_assets": ["asset-0"]})
    assert conflict.value.code == "asset_version_conflict"


def test_failed_suffix_retries_do_not_repeat_successful_prefixes():
    catalog = _catalog()
    compiled = compile_rule(_rule(100), _assets(100), catalog, catalog.lock,
                            scope={"readable_assets": list(_assets(100))},
                            settings=BatchSettings())
    outcomes = {}
    failed_items = []
    for item in compiled.document["items"]:
        for step in item["steps"]:
            failed = item["ordinal"] < 5 and step["local_id"] == "t3"
            outcomes[step["task_key"]] = {
                "status": "failed" if failed else "succeeded",
                "error": {"code": "provider_down"} if failed else {},
                "output": None if failed else {"asset_id": "out-" + step["task_key"], "media_type": "image"},
            }
        if item["ordinal"] < 5:
            failed_items.append(item["item_id"])
    redo = retry_plan(compiled.document, outcomes, failed_items)
    assert redo["item_count"] == 5
    assert redo["task_count"] == 5
    assert {step["local_id"] for item in redo["items"] for step in item["steps"]} == {"t3"}
    assert all(step["task_key"].startswith("t") and ":" not in step["task_key"] for item in redo["items"] for step in item["steps"])
    prices = {"compose_layers": 1}
    assert quote_of(redo, prices) == 5
    assert budget_allows(100, 0, 0, 90, 5, pending_quotes=10) is False
    assert budget_allows(100, 0, 0, 90, 5, pending_quotes=0) is True
    blocked = dict(outcomes)
    blocked[compiled.document["items"][0]["steps"][2]["task_key"]] = {"status": "blocked_by_model", "error": {"code": "blocked_by_model"}}
    with pytest.raises(CompileError) as denied:
        retry_plan(compiled.document, blocked, [compiled.document["items"][0]["item_id"]])
    assert denied.value.code == "blocked_by_model"


def test_dispatch_is_bounded_idempotent_and_keeps_unsettled_budget():
    ledger = DispatchLedger(limit=2)
    assert ledger.claim("a", credits=3, run_budget_left=10, run_inflight=0, per_run=2) == "reserved"
    assert ledger.claim("a", credits=3, run_budget_left=10, run_inflight=1, per_run=2) == "duplicate:reserved"
    assert ledger.ack("a") == "submitted"
    assert ledger.ack("a") == "duplicate:submitted"
    assert ledger.inflight == 1
    assert ledger.claim("b", credits=50, run_budget_left=10, run_inflight=1, per_run=2) == "budget_rejected"
    assert ledger.inflight == 1
    assert ledger.claim("c", credits=1, run_budget_left=10, run_inflight=1, per_run=2) == "reserved"
    assert ledger.claim("d", credits=1, run_budget_left=10, run_inflight=2, per_run=2) == "concurrency_rejected"
    assert ledger.recover() == ["c"]
    assert ledger.settle("a") == "settled"
    assert ledger.inflight == 1
    assert claim_decision(inflight=8, run_inflight=0, credits=1, budget_left=5, existing=None,
                          settings=BatchSettings(global_concurrency=8)) == "concurrency_rejected"
    progress = item_progress({"compiled": True, "item_count": 2, "items": [
        {"item_id": "a", "steps": [{"task_key": "a1"}]},
        {"item_id": "b", "steps": [{"task_key": "b1"}]},
    ]}, {"a1": "succeeded", "b1": "failed"})
    assert progress["completedItems"] == 1
    assert progress["settledItems"] == 2
    assert progress["processedItems"] == 2
    assert progress["failedCount"] == 1
    assert progress["skippedCount"] == 0
    assert progress["settled"] is True
    skipped = item_progress({"compiled": True, "item_count": 2, "items": [
        {"item_id": "a", "steps": [{"task_key": "a1"}]},
        {"item_id": "b", "steps": [{"task_key": "b1"}]},
    ]}, {"a1": "cancelled", "b1": "succeeded"}, {"a1": {"code": "no_subject"}, "b1": {}})
    assert skipped["completedItems"] == 1
    assert skipped["skippedCount"] == 1
    assert skipped["failedCount"] == 0
    cancelled = item_progress({"compiled": True, "item_count": 1, "items": [
        {"item_id": "a", "steps": [{"task_key": "a1"}]},
    ]}, {"a1": "cancelled"}, {"a1": {"code": "user_cancelled"}})
    assert cancelled["skippedCount"] == 0
    assert cancelled["completedItems"] == 0
    assert cancelled["settledItems"] == 1


def test_collection_keeps_global_task_keys_and_ignores_prompt_dollars():
    catalog = _catalog()
    assets = _assets(2)
    assets["$selection"] = [
        {"asset_id": "asset-0", "version": 3, "media_type": "image"},
        {"asset_id": "asset-1", "version": 3, "media_type": "image"},
    ]
    template = [
        {"id": "t1", "tool": "text_layout", "parameters": {"text": "price is $5"}},
        {"id": "t2", "tool": "compose_layers", "parameters": {"subject": "$t1.output"},
         "depends_on": ["t1"]},
    ]
    compiled = compile_rule(
        {"inputs_collection": "selected_images", "task_template": template},
        assets, catalog, catalog.lock, scope={"readable_assets": ["asset-0", "asset-1"]})
    assert [step.id for step in compiled.steps] == ["t1", "t2", "t3", "t4"]
    assert compiled.steps[0].parameters["text"] == "price is $5"
    assert compiled.steps[1].parameters["subject"] == "$t1.output"
    assert compiled.document["items"][0]["steps"][0]["task_key"] == "t1"
    assert compiled.document["items"][0]["steps"][0]["logical_key"].startswith("i:")
    with pytest.raises(CompileError) as unknown:
        compile_rule({"inputs_collection": "selected_images", "task_template": [
            {"id": "t1", "tool": "compose_layers", "parameters": {"subject": "$t9.output"}},
        ]}, assets, catalog, catalog.lock, scope={"readable_assets": ["asset-0", "asset-1"]})
    assert unknown.value.code == "invalid_reference"
    with pytest.raises(CompileError) as sizes:
        compile_rule({"inputs": [{"asset_id": "asset-0"}], "sizes": ["feed_square", "feed_square"], "task_template": template[:1]},
                     assets, catalog, catalog.lock, scope={"readable_assets": ["asset-0"]})
    assert sizes.value.code == "duplicate_size"


def test_explicit_batch_context_keeps_non_batch_cap():
    from backend.core.supervisor.context import ContextLimitError, build_context
    objects = [{"id": f"n{i}", "asset_id": f"asset-{i}", "kind": "image", "name": f"full description {i}" * 4}
               for i in range(30)]
    context = {"request": {"message": "批量", "selected_assets": [f"asset-{i}" for i in range(30)]},
               "scope": {"readable_assets": [f"asset-{i}" for i in range(30)]},
               "snapshot": {"objects": objects}}
    with pytest.raises(ContextLimitError):
        build_context(context, Settings(context_max_objects=24))
    built = build_context({**context, "request": {**context["request"], "batch": True}}, Settings(context_max_objects=24))
    assert built["dynamic"]["collections"]["selected_images"]["count"] == 30
    assert built["dynamic"]["canvas"]["selected"] == []
    assert "full description" not in str(built["dynamic"]["collections"])


def test_title_in_generate_prompt_and_unpriced_tool_are_rejected():
    catalog = _catalog()
    template = [
        {"id": "t1", "tool": "text_layout", "parameters": {"text": "夏季大促"}},
        {"id": "t2", "tool": "compose_layers", "parameters": {"subject": "$t1.output", "prompt": "海报写夏季大促"}, "depends_on": ["t1"]},
    ]
    with pytest.raises(CompileError) as title:
        compile_rule(_rule(1, template), _assets(1), catalog, catalog.lock, scope={"readable_assets": ["asset-0"]})
    assert title.value.code == "prompt_draws_title"
    class _Bare:
        def get_tool(self, name, version):
            return SimpleNamespace(available=True, price=SimpleNamespace(kind="unpriced", credits=None), version="1")

    with pytest.raises(CompileError) as unpriced:
        compile_rule({"inputs": [{"asset_id": "asset-0"}], "task_template": [_template()[0]]},
                     _assets(1), _Bare(), SimpleNamespace(version="1"), scope={"readable_assets": ["asset-0"]})
    assert unpriced.value.code == "tool_unpriced"


def test_batch_intent_sizes_and_v1_schema():
    from backend.core.batch.intent import apply_batch_intent
    from backend.core.batch.runtime import canvas_placement
    from backend.core.supervisor.tool_selection import planning_tools

    words = apply_batch_intent({"message": "全部抠图换白底", "selected_assets": ["a", "b"]})
    assert words["batch"] is True
    assert "sizes" not in words
    assert apply_batch_intent({"message": "不要批量全部处理", "selected_assets": ["a", "b"]})["batch"] is False
    compared = apply_batch_intent({"message": "比较这两张哪个更好", "selected_assets": ["a", "b"]})
    assert compared.get("batch") is not True
    sized = apply_batch_intent({"message": "这张做成方图和竖版", "selected_assets": ["a"]})
    assert sized["batch"] is True
    assert sized["sizes"] == ["feed_square", "story_portrait"]
    assert apply_batch_intent({"message": "抠图", "selected_assets": ["a"], "action": "batch"})["batch"] is True
    catalog = _catalog()
    old = planning_tools(catalog, {}, catalog.lock, prompt_version="supervisor-v1")
    new = planning_tools(catalog, {}, catalog.lock, prompt_version="supervisor-v2")
    assert all(tool["function"]["name"] != "for_each" for tool in old.tools)
    assert any(tool["function"]["name"] == "for_each" for tool in new.tools)
    leaf = canvas_placement({"mode": "right", "suppress_canvas": True}, tool="text_layout", step_id="t1",
                            batching=True, depended_ids=set())
    assert "suppress_canvas" not in leaf
    hidden = canvas_placement({"mode": "right", "suppress_canvas": False}, tool="cutout", step_id="t1",
                              batching=True, depended_ids={"t1"})
    assert hidden["suppress_canvas"] is True
    ordinary = canvas_placement({"mode": "right", "suppress_canvas": True}, tool="cutout", step_id="t1",
                                batching=False, depended_ids={"t1"})
    assert "suppress_canvas" not in ordinary
