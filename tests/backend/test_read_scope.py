"""Scope projection for read-only execution. No database and no provider."""
import json

from backend.agent.tools.canvas_read import READ_TOOLS
from backend.core.execution.engine import _read_gateway, scoped_canvas_raw


def test_scoped_canvas_drops_unauthorized_text_assets_and_prompts():
    raw = {"objects": {
        "ok": {"kind": "image", "x": 1, "y": 2, "width": 3, "height": 4, "assetId": "asset-ok", "text": "SECRET_TEXT"},
        "note": {"kind": "text", "x": 0, "y": 0, "width": 1, "height": 1, "text": "SECRET_TEXT"},
        "foreign": {"kind": "image", "x": 0, "y": 0, "width": 1, "height": 1, "assetId": "asset-no",
                    "src": "/api/assets/asset-no/content"},
    }, "order": ["note", "foreign", "ok"], "generations": {"g": {"prompt": "SECRET_PROMPT"}}}
    scoped = scoped_canvas_raw(raw, {"asset-ok"})
    dumped = json.dumps(scoped)
    assert list(scoped["objects"]) == ["ok"]
    assert scoped["objects"]["ok"]["assetId"] == "asset-ok"
    assert "text" not in scoped["objects"]["ok"]
    assert "SECRET_TEXT" not in dumped and "SECRET_PROMPT" not in dumped and "asset-no" not in dumped
    assert scoped_canvas_raw(raw, set())["objects"] == {}


def test_read_gateway_has_no_model_write_tool():
    names = set(_read_gateway().specs)
    assert {"read_canvas", "read_asset", "inspect_image"} <= names
    assert names <= {spec.name for spec in READ_TOOLS} | {"read_asset"}
    assert "detect_subject" not in names
    from backend.core.tools.contracts import ToolSettings
    assert _read_gateway().specs["read_asset"].timeout_s == ToolSettings().local_timeout_seconds


def test_read_gateway_timeout_follows_tool_sla():
    class _Execution:
        sla_seconds = 42.0

    class _Tool:
        name = "inspect_image"
        execution = _Execution()

    gateway = _read_gateway(_Tool())
    assert gateway.specs["inspect_image"].timeout_s == 42.0
    assert gateway.specs["read_canvas"].timeout_s == next(spec.timeout_s for spec in READ_TOOLS if spec.name == "read_canvas")


def test_document_text_requires_its_frozen_node_permission():
    raw = {"objects": {
        "text-ok": {"kind": "text", "text": "visible", "x": 0, "y": 0, "width": 1, "height": 1},
        "text-no": {"kind": "text", "text": "private"},
        "image-no": {"kind": "image", "assetId": "not-authorized"},
    }, "order": ["text-ok", "text-no", "image-no"]}
    projected = scoped_canvas_raw(raw, set(), {"text-ok", "image-no"})
    assert projected["order"] == ["text-ok"]
    assert projected["objects"]["text-ok"]["text"] == "visible"
    assert "private" not in str(projected)
    assert "not-authorized" not in str(projected)


def test_malformed_text_does_not_invalidate_other_readable_nodes():
    raw = {"objects": {"bad": {"kind": "text", "text": "missing geometry"},
                       "ok": {"kind": "text", "text": "valid", "x": 0, "y": 0, "width": 1, "height": 1}}}
    assert set(scoped_canvas_raw(raw, set(), {"bad", "ok"})["objects"]) == {"ok"}
