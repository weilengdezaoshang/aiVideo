"""Compiler rejects coordinate placement and only rewrites declared media ports."""
from __future__ import annotations

import copy

import pytest
from jsonschema import Draft202012Validator

from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.core.batch.compile import CompileError, compile_rule, restore
from backend.core.batch.presets import PRESETS, default_preset_table
from backend.core.composition.presets import PRESETS as COMPOSITION_PRESETS
from backend.core.tools import build_catalog
from backend.core.tools.catalog import ToolCatalog
from backend.core.tools.contracts import CatalogSnapshot, Execution, Port, Price, ToolDefinition
from backend.core.tools.definitions import object_schema


def _execution(binding=None) -> Execution:
    return Execution(adapter="fake", result_url_policy="authenticated_asset", sla_seconds=5,
                     binding=binding or {})


def _tool(name, properties, inputs, *, binding=None, required=None):
    return ToolDefinition(
        name=name, version="1", description=name,
        parameters=object_schema(properties, required if required is not None else []),
        result_schema=object_schema({"ok": {"type": "boolean"}}),
        inputs=inputs, outputs=[Port(path="output", media_type="image")],
        mode="deterministic", price=Price(kind="fixed", credits=1), limits={},
        execution=_execution(binding), failures=[])


def _loose():
    image = {"anyOf": [object_schema({"asset_id": {"type": "string"}, "media_type": {"const": "image"}}),
                       {"type": "string"}]}
    return ToolCatalog(CatalogSnapshot(version="sec", tools=[
        _tool("detect_subject", {"image": image, "width": {"type": "integer"}, "height": {"type": "integer"}},
              [Port(path="image", media_type="image")]),
        _tool("compose_layers", {"subject": {"type": "string"}, "prompt": {"type": "string"},
                                 "width": {"type": "integer"}, "height": {"type": "integer"}},
              [Port(path="subject", media_type="image", required=False)]),
        _tool("text_layout", {"copy": {"type": "string"}, "text": {"type": "string"}}, []),
    ]))


def _assets():
    return {"asset-0": {"version": 3, "sha256": "ab" * 32}, "asset-1": {"version": 3, "sha256": "cd" * 32}}


def _scope():
    return {"readable_assets": ["asset-0", "asset-1"], "writable_regions": ["board"],
            "objects": {"n0": {"assetId": "asset-0"}}}


def test_literal_prompt_is_not_rewritten_but_media_ports_are():
    catalog = _loose()
    template = [
        {"id": "t1", "tool": "text_layout", "parameters": {"copy": "$t1.output", "text": "kept"}},
        {"id": "t2", "tool": "compose_layers", "parameters": {
            "prompt": "notes $t9.output", "subject": "$t1.output"}, "depends_on": ["t1"]},
    ]
    compiled = compile_rule(
        {"inputs": [{"asset_id": "asset-0"}, {"asset_id": "asset-1"}], "task_template": template},
        _assets(), catalog, catalog.lock, scope=_scope())
    assert compiled.steps[0].parameters["copy"] == "$t1.output"
    assert compiled.steps[2].parameters["copy"] == "$t1.output"
    assert compiled.steps[1].parameters["prompt"] == "notes $t9.output"
    assert compiled.steps[3].parameters["prompt"] == "notes $t9.output"
    assert compiled.steps[1].parameters["subject"] == "$t1.output"
    assert compiled.steps[3].parameters["subject"] == "$t3.output"
    assert compiled.steps[3].depends_on == ["t3"]
    image = [
        {"id": "t1", "tool": "detect_subject", "parameters": {"image": {"$item": "asset"}, "width": 64, "height": 64}},
        {"id": "t2", "tool": "compose_layers", "parameters": {"subject": "$t1.output", "prompt": "keep"},
         "depends_on": ["t1"]},
    ]
    mapped = compile_rule({"inputs": [{"asset_id": "asset-0"}, {"asset_id": "asset-1"}], "task_template": image},
                          _assets(), catalog, catalog.lock, scope=_scope())
    assert mapped.steps[0].parameters["image"] == {"asset_id": "asset-0", "media_type": "image"}
    assert mapped.steps[0].parameters["width"] == 64
    assert mapped.steps[2].parameters["image"]["asset_id"] == "asset-1"
    assert mapped.steps[3].parameters["subject"] == "$t3.output"
    assert mapped.document["items"][0]["sha256"] == "ab" * 32
    with pytest.raises(CompileError) as unknown:
        compile_rule({"inputs": [{"asset_id": "asset-0"}], "task_template": [
            {"id": "t1", "tool": "compose_layers", "parameters": {"subject": "$t9.output"}}]},
            _assets(), catalog, catalog.lock, scope=_scope())
    assert unknown.value.code == "invalid_reference"
    with pytest.raises(CompileError) as local:
        compile_rule({"inputs": [{"asset_id": "asset-0"}], "task_template": [
            {"id": "t1", "tool": "text_layout", "parameters": {"copy": {"$item": "asset"}}}]},
            _assets(), catalog, catalog.lock, scope=_scope())
    assert local.value.code == "invalid_local_ref"
    with pytest.raises(CompileError) as cross:
        compile_rule({"inputs": [{"asset_id": "asset-0"}], "task_template": [
            {"id": "t1", "tool": "compose_layers", "parameters": {"subject": "$t1.output"}, "depends_on": ["t9"]}]},
            _assets(), catalog, catalog.lock, scope=_scope())
    assert cross.value.code == "invalid_reference"


def test_placement_rejects_coordinates_and_keeps_output_size():
    catalog = _loose()
    template = [{"id": "t1", "tool": "detect_subject", "parameters": {
        "image": {"$item": "asset"}, "width": 1080, "height": 1920},
        "placement": {"mode": "region", "region": {"x": 1, "y": 2, "width": 3, "height": 4}}}]
    with pytest.raises(CompileError) as coords:
        compile_rule({"inputs": [{"asset_id": "asset-0"}], "task_template": template},
                     _assets(), catalog, catalog.lock, scope=_scope())
    assert coords.value.code == "invalid_placement"
    placed = compile_rule({"inputs": [{"asset_id": "asset-0"}], "task_template": [{
        "id": "t1", "tool": "detect_subject",
        "parameters": {"image": {"$item": "asset"}, "width": 1080, "height": 1920},
        "placement": {"mode": "right", "anchor_id": "n0"}}]},
        _assets(), catalog, catalog.lock, scope=_scope())
    assert placed.steps[0].parameters["width"] == 1080
    assert placed.steps[0].parameters["height"] == 1920
    assert placed.steps[0].placement == {"mode": "right", "anchor_id": "n0"}
    with pytest.raises(CompileError) as suppressed:
        compile_rule({"inputs": [{"asset_id": "asset-0"}], "task_template": [{
            "id": "t1", "tool": "detect_subject",
            "parameters": {"image": {"$item": "asset"}, "width": 64, "height": 64},
            "placement": {"mode": "right", "suppress_canvas": True}}]},
            _assets(), catalog, catalog.lock, scope=_scope())
    assert suppressed.value.code == "invalid_placement"


def test_three_sizes_match_real_tool_schemas_and_restore_keeps_v1():
    catalog = build_catalog(Catalog(CatalogConfig(version="sec-compose", environment="development",
                                                  enabled=True, capabilities=[])), "sec-compose")
    compose = catalog.get_tool("compose_layers", catalog.lock.version)
    text = catalog.get_tool("text_layout", catalog.lock.version)
    template = [
        {"id": "t1", "tool": "text_layout", "parameters": {
            "copy": "月圆人团圆", "font_id": "noto-sans-sc", "size": 48, "color": "#112233",
            "alignment": "center", "region": "title", "preset": {"$size": "id"}}},
        {"id": "t2", "tool": "compose_layers", "parameters": {
            "background": {"kind": "color", "color": "#FFFFFF"}, "text_layers": ["$t1.output"],
            "fit": "contain", "width": {"$size": "width"}, "height": {"$size": "height"}},
         "depends_on": ["t1"]},
    ]
    rule = {"inputs": [{"asset_id": "asset-0"}], "sizes": ["feed_square", "story_portrait", "banner_landscape"],
            "task_template": template}
    compiled = compile_rule(rule, _assets(), catalog, catalog.lock, scope=_scope())
    assert len(compiled.steps) == 6
    widths = []
    for step in compiled.steps:
        tool = text if step.tool == "text_layout" else compose
        Draft202012Validator(tool.parameters).validate(step.parameters)
        if step.tool == "text_layout":
            assert step.parameters["copy"] == "月圆人团圆"
            assert step.parameters["preset"] in {"feed_square", "story_portrait", "banner_landscape"}
            assert "preset_id" not in step.parameters
            assert "canvas" not in step.parameters
        else:
            widths.append((step.parameters["width"], step.parameters["height"]))
            assert "preset" not in step.parameters
    assert widths == [(1080, 1080), (1080, 1920), (1920, 1080)]
    assert compiled.document["preset_version"] == default_preset_table()[0]
    original = copy.deepcopy(compiled.document)
    COMPOSITION_PRESETS_SNAPSHOT = dict(COMPOSITION_PRESETS)
    try:
        COMPOSITION_PRESETS.clear()
        COMPOSITION_PRESETS["feed_square"] = (11, 11)
        PRESETS.clear()
        PRESETS["feed_square"] = {"width": 11, "height": 11}
        restored = restore(original)
    finally:
        COMPOSITION_PRESETS.clear()
        COMPOSITION_PRESETS.update(COMPOSITION_PRESETS_SNAPSHOT)
        PRESETS.clear()
        PRESETS.update({key: {"width": pair[0], "height": pair[1]} for key, pair in COMPOSITION_PRESETS.items()})
    assert restored.steps[1].parameters["width"] == 1080
    assert restored.document["sizes"][0]["width"] == 1080
    with pytest.raises(CompileError) as duplicate:
        compile_rule({**rule, "sizes": ["feed_square", "feed_square"]}, _assets(), catalog, catalog.lock, scope=_scope())
    assert duplicate.value.code == "duplicate_size"
    with pytest.raises(CompileError) as unknown:
        compile_rule({**rule, "sizes": ["not_a_preset"]}, _assets(), catalog, catalog.lock, scope=_scope())
    assert unknown.value.code == "invalid_size"
    with pytest.raises(CompileError) as boolean:
        compile_rule({**rule, "sizes": [{"id": "odd", "width": True, "height": 1080}]},
                     _assets(), catalog, catalog.lock, scope=_scope())
    assert boolean.value.code == "invalid_size"
    with pytest.raises(CompileError) as huge:
        compile_rule({**rule, "sizes": [{"id": "huge", "width": 9000, "height": 1080}]},
                     _assets(), catalog, catalog.lock, scope=_scope())
    assert huge.value.code == "invalid_size"
    with pytest.raises(CompileError) as forged:
        compile_rule({**rule, "compiled": True}, _assets(), catalog, catalog.lock, scope=_scope())
    assert forged.value.code == "compiled_flag_rejected"
