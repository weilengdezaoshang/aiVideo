"""Planning tool list and model call conversion. No provider, database, or tool execution."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from backend.agent.admission.tool_types import TypedTask, review_tool_types
from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.core.supervisor.contracts import Plan, Settings
from backend.core.supervisor.reviewer import review
from backend.core.supervisor.tool_calls import ConversionError, convert_tool_calls
from backend.core.supervisor.tool_selection import planning_tools
from backend.core.tools.catalog import ToolCatalog
from backend.core.tools.contracts import (
    CatalogSnapshot, Execution, Port, Price, ToolDefinition, VersionLock)
from backend.core.tools.definitions import build_catalog, object_schema, reference_schema


def dev_catalog():
    raw = json.loads(Path("deploy/host-catalog.development.json").read_text())
    raw["enabled"] = True
    for cap in raw["capabilities"]:
        cap["enabled"] = True
    return build_catalog(Catalog(CatalogConfig.model_validate(raw)), "tools-v1")


def _execution() -> Execution:
    return Execution(adapter="test", result_url_policy="authenticated_asset", sla_seconds=5)


def _tool(name, description, params, *, mode="generate", inputs=None, outputs=None, groups=None,
          available=True):
    outputs = outputs or [Port(path="output", media_type="image")]
    return ToolDefinition(
        name=name, version="1", description=description, parameters=params,
        result_schema=object_schema({"ok": {"type": "boolean"}}), inputs=inputs or [],
        outputs=outputs, mode=mode, price=Price(kind="fixed", credits=0), limits={},
        execution=_execution(), failures=[], available=available,
        unavailable_reason=None if available else "hidden", rollout_groups=groups or [])


def _catalog(tools, version="synthetic-v1"):
    return ToolCatalog(CatalogSnapshot(version=version, tools=tools))


def _image(asset="asset-a"):
    return {"asset_id": asset, "media_type": "image"}


def _filled(tool, **overrides):
    params = {}
    for name, schema in tool.parameters["properties"].items():
        if name == "_plan":
            continue
        if "enum" in schema:
            params[name] = schema["enum"][0]
        elif schema.get("type") == "integer":
            params[name] = schema.get("minimum", 1)
        elif schema.get("type") == "number":
            params[name] = schema.get("minimum", 1)
        elif schema.get("type") == "string":
            params[name] = "红色"
        elif "anyOf" in schema or name in {"image", "mask"}:
            params[name] = _image()
    params.update(overrides)
    return params


def _context(message="把图改成红色再做成视频", tools=None):
    scope = {"readable_assets": ["asset-a"], "writable_regions": ["region-1"]}
    if tools is not None:
        scope["tools"] = tools
    return {"request": {"message": message}, "scope": scope, "invariants": {"frozen": True},
            "snapshot": {"objects": {"node-1": {"assetId": "asset-a"}}}}


def _convert(catalog, calls, **kwargs):
    settings = kwargs.pop("settings", Settings(planner_tool_threshold=200))
    user = kwargs.pop("user", {"rollout_group": "beta"})
    return convert_tool_calls(
        calls, context=kwargs.pop("context", _context()), catalog=catalog, user=user,
        version_lock=catalog.lock, settings=settings, **kwargs)


def test_edit_then_video_keeps_order_goal_and_raw_calls():
    catalog = dev_catalog()
    message = "把图改成红色再做成视频"
    edit = catalog.get_tool("image_edit_preview", catalog.lock.version)
    video = catalog.get_tool("frames_to_video_preview", catalog.lock.version)
    calls = [
        {"id": "c1", "name": "image_edit_preview", "arguments": _filled(edit, image=_image())},
        {"id": "c2", "name": "frames_to_video_preview", "arguments": _filled(
            video, frames=[_image(), "$t1.output"], prompt="红色动态")},
    ]
    result = _convert(catalog, calls, context=_context(message))
    assert result.plan.writer == "llm"
    assert result.plan.goal == message
    assert [step.tool for step in result.plan.steps] == ["image_edit_preview", "frames_to_video_preview"]
    assert result.plan.steps[1].depends_on == ["t1"]
    assert result.plan.steps[0].id == "t1" and result.plan.steps[1].id == "t2"
    assert result.raw_output["calls"] == calls
    assert result.plan.steps[1].parameters["duration_seconds"] == video.parameters["properties"]["duration_seconds"]["enum"][0]


def test_nested_and_explicit_dependencies_and_mask_chain_pass_review():
    catalog = dev_catalog()
    detect = catalog.get_tool("detect_subject", catalog.lock.version)
    cutout = catalog.get_tool("cutout", catalog.lock.version)
    message = "抠出前景"
    calls = [
        {"id": "c1", "name": "detect_subject", "arguments": {
            "image": _image(), "_plan": {"placement": {"mode": "right", "anchor_id": "node-1"}}}},
        {"id": "c2", "name": "cutout", "arguments": {
            "image": _image(), "mask": "$t1.candidates[0].mask",
            "_plan": {"depends_on": ["t1"], "placement": {"mode": "below", "anchor_id": "node-1"}}}},
    ]
    result = _convert(catalog, calls, context=_context(message, ["detect_subject", "cutout"]))
    assert result.plan.steps[1].depends_on == ["t1"]
    Draft202012Validator(detect.parameters).validate(result.plan.steps[0].parameters)
    Draft202012Validator(cutout.parameters).validate(result.plan.steps[1].parameters)
    assert "_plan" not in result.plan.steps[1].parameters
    typed = review_tool_types(
        [TypedTask(id=step.id, tool=step.tool, parameters=step.parameters, depends_on=step.depends_on)
         for step in result.plan.steps], catalog, VersionLock.model_validate(catalog.lock.model_dump()))
    assert typed.issues == []
    output_calls = [
        calls[0],
        {"id": "c2", "name": "cutout", "arguments": {
            "image": _image(), "mask": "$t1.output",
            "_plan": {"placement": {"mode": "below", "anchor_id": "node-1"}}}},
    ]
    reviewed = _convert(catalog, output_calls, context=_context(message, ["detect_subject", "cutout"]))
    run = SimpleNamespace(
        user_id="user-1", credits_limit=100, credits_spent=0,
        scope={"tools": ["detect_subject", "cutout"], "readable_assets": ["asset-a"], "writable_regions": []},
        invariants={}, version_lock={"tool_catalog": catalog.lock.model_dump()},
        snapshot={"request": {"message": message}, "assets": [
            {"asset_id": "asset-a", "owner_id": "user-1", "kind": "image"}],
            "objects": {"node-1": {"assetId": "asset-a"}}})
    verdict = review(reviewed.plan, run, catalog, 100)
    assert verdict["verdict"] == "pass", verdict["reasons"]


def test_read_tool_scope_and_foreign_asset_and_coordinates():
    catalog = dev_catalog()
    read = catalog.get_tool("read_asset", catalog.lock.version)
    ok = _convert(catalog, [{"id": "r", "name": "read_asset", "arguments": _filled(read, asset_id="asset-a")}])
    assert ok.plan.steps[0].tool == "read_asset"
    assert read.outputs[0].media_type == "text"
    with pytest.raises(ConversionError) as foreign:
        _convert(catalog, [{"id": "r", "name": "read_asset", "arguments": _filled(read, asset_id="other-user")}])
    assert foreign.value.code == "asset_out_of_scope"
    edit = catalog.get_tool("image_edit_preview", catalog.lock.version)
    with pytest.raises(ConversionError) as coords:
        _convert(catalog, [{"id": "e", "name": "image_edit_preview", "arguments": {
            **_filled(edit), "_plan": {"placement": {"mode": "right", "anchor_id": "node-1", "x": 12}}}}])
    assert coords.value.code == "coordinates_rejected"
    with pytest.raises(ConversionError) as mode:
        _convert(catalog, [{"id": "e", "name": "image_edit_preview", "arguments": {
            **_filled(edit), "_plan": {"placement": {"mode": "center", "anchor_id": "node-1"}}}}])
    assert mode.value.code == "invalid_placement"
    with pytest.raises(ConversionError) as region:
        _convert(catalog, [{"id": "e", "name": "image_edit_preview", "arguments": {
            **_filled(edit), "_plan": {"placement": {"mode": "region", "region_id": "outside"}}}}])
    assert region.value.code == "placement_unauthorized"


def test_cycles_unknown_refs_and_duplicate_ids_are_rejected():
    catalog = dev_catalog()
    detect = catalog.get_tool("detect_subject", catalog.lock.version)
    base = _filled(detect, image=_image())
    placed = {"_plan": {"placement": {"mode": "right", "anchor_id": "node-1"}}}
    with pytest.raises(ConversionError) as cycle:
        _convert(catalog, [
            {"id": "a", "name": "detect_subject", "arguments": {**base, "image": "$t2.output", **placed}},
            {"id": "b", "name": "detect_subject", "arguments": {**base, "image": "$t1.output", **placed}},
        ])
    assert cycle.value.code == "dependency_cycle"
    with pytest.raises(ConversionError) as unknown:
        _convert(catalog, [{"id": "a", "name": "detect_subject", "arguments": {**base, "image": "$t9.output", **placed}}])
    assert unknown.value.code == "invalid_reference"
    with pytest.raises(ConversionError) as itself:
        _convert(catalog, [{"id": "a", "name": "detect_subject", "arguments": {**base, "image": "$t1.output", **placed}}])
    assert itself.value.code == "self_dependency"
    with pytest.raises(ConversionError) as missing:
        _convert(catalog, [{"id": "a", "name": "not_a_tool", "arguments": {}}])
    assert missing.value.code == "unknown_tool"
    with pytest.raises(ConversionError) as dup:
        _convert(catalog, [
            {"id": "same", "name": "detect_subject", "arguments": base},
            {"id": "same", "name": "detect_subject", "arguments": base},
        ])
    assert dup.value.code == "duplicate_call_id"


def test_form_chat_and_invariant_proposal_do_not_queue_hidden_work():
    catalog = dev_catalog()
    message = "只告诉我这张图是什么，时长必须是四秒"
    edit = catalog.get_tool("image_edit_preview", catalog.lock.version)
    form = {"id": "need-duration", "fields": [
        {"id": "duration", "type": "choice", "label": "时长", "required": True, "choices": ["四秒", "八秒"]}]}
    mixed = _convert(catalog, [
        {"id": "f", "name": "ask_user_form", "arguments": form},
        {"id": "e", "name": "image_edit_preview", "arguments": _filled(edit)},
    ], context=_context(message))
    assert mixed.plan.result_type == "clarify"
    assert [step.kind for step in mixed.plan.steps] == ["ask_user"]
    assert mixed.plan.control_audit.form.fields[0].id == "duration"
    chat = _convert(catalog, [{"id": "a", "name": "answer_only", "arguments": {"answer": "这是说明。"}}],
                    context=_context(message))
    assert chat.plan.result_type == "answer_only" and chat.plan.steps == []
    assert chat.plan.answer == "这是说明。"
    proposal = _convert(catalog, [
        {"id": "s", "name": "set_invariants", "arguments": {
            "invariants": {"deliverable": {"count": 1}}, "source_quote": "时长必须是四秒",
            "source": "user", "location": "message"}},
        {"id": "a", "name": "answer_only", "arguments": {"answer": "记下了。"}},
    ], context=_context(message))
    assert proposal.invariants == {"deliverable": {"count": 1}}
    assert proposal.plan.control_audits[0].evidence[0].quote == "时长必须是四秒"
    frozen = {"frozen": True}
    context = _context(message)
    context["invariants"] = frozen
    _convert(catalog, [
        {"id": "s", "name": "set_invariants", "arguments": {
            "invariants": {"deliverable": {"count": 2}}, "source_quote": "时长必须是四秒",
            "source": "user", "location": "message"}},
        {"id": "a", "name": "answer_only", "arguments": {"answer": "记下了。"}},
    ], context=context)
    assert context["invariants"] == {"frozen": True}
    with pytest.raises(ConversionError) as alone:
        _convert(catalog, [{"id": "s", "name": "set_invariants", "arguments": {
            "invariants": {"deliverable": {"count": 1}}, "source_quote": "时长必须是四秒",
            "source": "user", "location": "message"}}], context=_context(message))
    assert alone.value.code == "invariants_without_action"
    with pytest.raises(ConversionError) as blend:
        _convert(catalog, [
            {"id": "a", "name": "answer_only", "arguments": {"answer": "好"}},
            {"id": "e", "name": "image_edit_preview", "arguments": _filled(edit)},
        ])
    assert blend.value.code == "answer_only_mixed_with_generation"
    with pytest.raises(ConversionError) as html:
        _convert(catalog, [{"id": "f", "name": "ask_user_form", "arguments": {
            "id": "x", "fields": [{"id": "a", "type": "text", "label": "<b>时长</b>", "required": True}]}}])
    assert html.value.code == "form_html_rejected"
    with pytest.raises(ValidationError):
        Plan(goal="生成", result_type="generate", steps=[])


def test_rollout_scope_and_membership_do_not_publish_hidden_tools():
    catalog = dev_catalog()
    tools = [tool.model_copy(update={"rollout_groups": ["beta"]}) if tool.name == "cutout" else tool
             for tool in catalog.snapshot.tools]
    catalog = ToolCatalog(CatalogSnapshot(version="gray", tools=tools))
    hidden = planning_tools(catalog, {"membership": "vip", "credits": 999}, catalog.lock,
                            settings=Settings(planner_tool_threshold=200))
    shown = planning_tools(catalog, {"rollout_group": "beta", "credits": 0}, catalog.lock,
                           settings=Settings(planner_tool_threshold=200))
    assert "cutout" not in {item["function"]["name"] for item in hidden.tools}
    assert "cutout" in {item["function"]["name"] for item in shown.tools}
    assert {item["function"]["name"] for item in hidden.tools} >= {"ask_user_form", "answer_only", "set_invariants"}
    assert "spawn_helper" not in {item["function"]["name"] for item in shown.tools}
    scoped = planning_tools(catalog, {"rollout_group": "beta"}, catalog.lock, scope={"tools": ["read_asset"]},
                            settings=Settings(planner_tool_threshold=200))
    names = [item["function"]["name"] for item in scoped.tools]
    assert names[0] == "read_asset"
    assert set(names[-3:]) == {"ask_user_form", "answer_only", "set_invariants"}


def test_over_threshold_retrieval_closes_mask_chain_and_records_versions():
    tools = []
    for index in range(24):
        tools.append(_tool(f"filler_{index}", f"无关填充工具 {index}", object_schema(
            {"prompt": {"type": "string", "minLength": 1}})))
    tools.append(_tool(
        "detect_subject", "识别图片前景候选并返回临时蒙版。",
        object_schema({"image": reference_schema("image")}, required=["image"]), mode="read_only",
        inputs=[Port(path="image", media_type="image")],
        outputs=[Port(path="output", media_type="mask")]))
    tools.append(_tool(
        "cutout", "抠图并按蒙版合成新图片。没有蒙版时先使用 detect_subject。",
        object_schema({"image": reference_schema("image"), "mask": reference_schema("mask")}),
        mode="deterministic",
        inputs=[Port(path="image", media_type="image"), Port(path="mask", media_type="mask")],
        outputs=[Port(path="output", media_type="image")]))
    tools.append(_tool("spawn_helper", "不可对模型开放的帮手。", object_schema({"prompt": {"type": "string"}})))
    catalog = _catalog(tools)
    settings = Settings(planner_tool_threshold=20, planner_tool_candidates=1)
    listed = planning_tools(catalog, {}, catalog.lock, query="请抠图并按蒙版合成", settings=settings)
    names = [item["function"]["name"] for item in listed.tools]
    assert listed.retrieved is True
    assert listed.retrieval_version == "bm25-v1"
    assert listed.catalog_version == catalog.lock.version
    assert listed.catalog_hash == catalog.lock.content_hash
    assert "cutout" in names and "detect_subject" in names
    assert "spawn_helper" not in names
    assert names[-3:] == ["ask_user_form", "answer_only", "set_invariants"]
    assert len([name for name in names if name.startswith("filler_")]) == 0


def test_missing_video_duration_becomes_a_form_and_catalog_is_not_rewritten():
    catalog = dev_catalog()
    before = catalog.snapshot.model_dump(mode="json")
    video = catalog.get_tool("frames_to_video_preview", catalog.lock.version)
    arguments = _filled(video, frames=[_image(), _image()])
    arguments.pop("duration_seconds")
    result = _convert(catalog, [{"id": "v", "name": "frames_to_video_preview", "arguments": arguments}])
    assert result.plan.result_type == "clarify"
    assert result.plan.steps[0].kind == "ask_user"
    assert result.plan.steps[0].tool is None
    form = result.plan.steps[0].parameters["form"]
    assert form["fields"][0]["id"] == "duration_seconds"
    assert all(step.kind != "call_tool" for step in result.plan.steps)
    assert "duration_seconds" not in form["fields"][0] or form["fields"][0].get("default") is None
    listed = planning_tools(catalog, {}, catalog.lock, settings=Settings(planner_tool_threshold=200))
    listed.tools[0]["function"]["parameters"]["properties"]["prompt"] = {"type": "integer"}
    assert catalog.snapshot.model_dump(mode="json") == before
    assert "_plan" in listed.tools[0]["function"]["parameters"]["properties"]
    assert "_plan" not in before["tools"][0]["parameters"]["properties"]


def test_new_image_and_read_tool_do_not_require_an_anchor_node():
    catalog = dev_catalog()
    preview = catalog.get_tool("text_to_image_preview", catalog.lock.version)
    context = _context("画一张红色海报", [])
    context["scope"] = {"readable_assets": [], "writable_regions": [], "tools": ["text_to_image_preview", "read_asset"]}
    context["snapshot"] = {"objects": {}}
    result = _convert(catalog, [{"id": "n", "name": "text_to_image_preview", "arguments": _filled(preview)}],
                      context=context)
    assert result.plan.steps[0].placement == {"mode": "right"}
    assert "anchor_id" not in result.plan.steps[0].placement
    read = catalog.get_tool("read_asset", catalog.lock.version)
    read_context = _context("看看这张", ["read_asset"])
    read_result = _convert(catalog, [{"id": "r", "name": "read_asset", "arguments": _filled(read, asset_id="asset-a")}],
                           context=read_context)
    assert read_result.plan.steps[0].placement == {}


def test_anchor_must_be_a_node_mapped_to_an_authorized_asset():
    catalog = dev_catalog()
    edit = catalog.get_tool("image_edit_preview", catalog.lock.version)
    context = _context("把图改成红色", ["image_edit_preview"])
    context["snapshot"] = {"objects": {
        "node-real": {"assetId": "asset-a"},
        "node-foreign": {"assetId": "other-user"},
    }}
    ok = _convert(catalog, [{"id": "e", "name": "image_edit_preview", "arguments": {
        **_filled(edit), "_plan": {"placement": {"mode": "right", "anchor_id": "node-real"}}}}], context=context)
    assert ok.plan.steps[0].placement["anchor_id"] == "node-real"
    with pytest.raises(ConversionError) as asset_uuid:
        _convert(catalog, [{"id": "e", "name": "image_edit_preview", "arguments": {
            **_filled(edit), "_plan": {"placement": {"mode": "right", "anchor_id": "asset-a"}}}}], context=context)
    assert asset_uuid.value.code == "placement_unauthorized"
    with pytest.raises(ConversionError) as foreign:
        _convert(catalog, [{"id": "e", "name": "image_edit_preview", "arguments": {
            **_filled(edit), "_plan": {"placement": {"mode": "right", "anchor_id": "node-foreign"}}}}], context=context)
    assert foreign.value.code == "placement_unauthorized"
    missing_scope = _context("把图改成红色", ["image_edit_preview"])
    missing_scope["scope"] = {"tools": ["image_edit_preview"]}
    with pytest.raises(ConversionError) as open_scope:
        _convert(catalog, [{"id": "e", "name": "image_edit_preview", "arguments": {
            **_filled(edit), "_plan": {"placement": {"mode": "right", "anchor_id": "node-1"}}}}], context=missing_scope)
    assert open_scope.value.code == "placement_unauthorized"


def test_control_schemas_reject_loose_values_and_duplicate_controls_merge_or_fail():
    catalog = dev_catalog()
    message = "时长必须是四秒"
    with pytest.raises(ConversionError):
        _convert(catalog, [{"id": "f", "name": "ask_user_form", "arguments": {
            "id": "x", "fields": [{"id": "", "type": "text", "label": "时长", "required": "yes"}]}}],
            context=_context(message))
    with pytest.raises(ConversionError):
        _convert(catalog, [{"id": "f", "name": "ask_user_form", "arguments": {
            "id": "x", "extra": True, "fields": [{"id": "a", "type": "text", "label": "时长"}]}}],
            context=_context(message))
    with pytest.raises(ConversionError):
        _convert(catalog, [{"id": "a", "name": "answer_only", "arguments": {"answer": "好"}},
                           {"id": "b", "name": "answer_only", "arguments": {"answer": "也好"}}],
                 context=_context(message))
    kept = _convert(catalog, [{"id": "f", "name": "ask_user_form", "arguments": {
        "id": "need", "fields": [{"id": "duration", "type": "number", "label": "时长", "required": True, "default": 4}]}}],
        context=_context(message))
    assert kept.plan.steps[0].parameters["form"]["fields"][0]["default"] == 4
    merged = _convert(catalog, [
        {"id": "s1", "name": "set_invariants", "arguments": {
            "invariants": {"deliverable": {"count": 1}}, "source_quote": "时长必须是四秒",
            "source": "user", "location": "message"}},
        {"id": "s2", "name": "set_invariants", "arguments": {
            "invariants": {"deliverable": {"count": 1}}, "source_quote": "时长必须是四秒",
            "source": "user", "location": "message"}},
        {"id": "a", "name": "answer_only", "arguments": {"answer": "记下了。"}},
    ], context=_context(message))
    assert merged.invariants["deliverable"]["count"] == 1
    with pytest.raises(ConversionError) as clash:
        _convert(catalog, [
            {"id": "s1", "name": "set_invariants", "arguments": {
                "invariants": {"deliverable": {"count": 1}}, "source_quote": "时长必须是四秒",
                "source": "user", "location": "message"}},
            {"id": "s2", "name": "set_invariants", "arguments": {
                "invariants": {"deliverable": {"count": 2}}, "source_quote": "时长必须是四秒",
                "source": "user", "location": "message"}},
            {"id": "a", "name": "answer_only", "arguments": {"answer": "记下了。"}},
        ], context=_context(message))
    assert clash.value.code == "conflicting_controls"


def test_dollar_text_downstream_default_and_replace_need_an_anchor():
    catalog = dev_catalog()
    edit = catalog.get_tool("image_edit_preview", catalog.lock.version)
    first = _filled(edit, prompt="海报售价$100")
    second = _filled(edit, image="$t1.output", prompt="再便宜$100")
    result = _convert(catalog, [
        {"id": "c1", "name": "image_edit_preview", "arguments": {
            **first, "_plan": {"placement": {"mode": "right", "anchor_id": "node-1"}}}},
        {"id": "c2", "name": "image_edit_preview", "arguments": {
            **second, "_plan": {"placement": {"mode": "below"}}}},
    ])
    assert result.plan.steps[0].parameters["prompt"] == "海报售价$100"
    assert result.plan.steps[1].depends_on == ["t1"]
    assert result.plan.steps[1].placement == {"mode": "below"}
    assert "anchor_id" not in result.plan.steps[1].placement
    with pytest.raises(ConversionError) as replaced:
        _convert(catalog, [{"id": "e", "name": "image_edit_preview", "arguments": {
            **first, "_plan": {"placement": {"mode": "replace"}}}}])
    assert replaced.value.code == "placement_unauthorized"
    read = catalog.get_tool("read_asset", catalog.lock.version)
    read_result = _convert(catalog, [{"id": "r", "name": "read_asset", "arguments": {
        **_filled(read, asset_id="asset-a"), "_plan": {"placement": {"mode": "right", "anchor_id": "node-1"}}}}])
    assert read_result.plan.steps[0].placement == {}


def test_bm25_splits_tool_names_and_reads_k1_from_settings():
    tools = [_tool(f"filler_{index}", "无关", object_schema({"prompt": {"type": "string"}})) for index in range(24)]
    tools.append(_tool("image_edit_preview", "edit an image", object_schema({"prompt": {"type": "string"}})))
    catalog = _catalog(tools)
    settings = Settings(planner_tool_threshold=20, planner_tool_candidates=1, planner_bm25_k1=1.2, planner_bm25_b=0.5)
    listed = planning_tools(catalog, {}, catalog.lock, query="please edit the image", settings=settings)
    assert listed.tools[0]["function"]["name"] == "image_edit_preview"
