import hashlib
import io
import json

import pytest
from PIL import Image

from backend.agent.tools import ToolError, execute_tool, project_snapshot, tool_schemas


def canvas():
    return {"id": "doc", "name": "画布", "revision": 3, "order": ["text", "image"], "objects": {
        "image": {"id": "image", "kind": "image", "x": 10, "y": 20, "width": 999, "height": 999,
                  "assetId": "asset", "src": "https://untrusted.example/image", "cutout": {
                      "maskAssetId": "secret-mask"}, "recognition": "secret-recognition"},
        "text": {"id": "text", "kind": "text", "x": 0, "y": 0, "width": 100, "height": 100,
                 "text": "轻盈出发", "strokes": ["secret-stroke"]}},
        "chat": [{"maskUrl": "secret-mask"}], "mask": "secret-mask"}


def bind_image(tmp_path):
    picture = Image.new("RGB", (17, 23), "blue")
    stream = io.BytesIO()
    picture.save(stream, format="PNG")
    content = stream.getvalue()
    (tmp_path / "actual.png").write_bytes(content)
    return {"asset": {"storage_key": "actual.png", "sha256": hashlib.sha256(content).hexdigest(),
                      "bytes": len(content), "ext": "png"}}


def test_snapshot_projects_allowed_fields_and_keeps_original_order(tmp_path):
    raw = canvas()
    snapshot = project_snapshot(raw, ["image"])
    dumped = json.dumps(snapshot)
    assert "secret-" not in dumped and "https://" not in dumped and "chat" not in dumped
    raw["objects"]["text"]["text"] = "已变化"
    assert snapshot["objects"]["text"]["text"] == "轻盈出发"
    result = execute_tool("read_canvas", {}, snapshot, {}, tmp_path)
    assert result["objectCount"] == 2 and result["selectedObjectIds"] == ["image"]
    assert result["revision"] == 3
    page = execute_tool("list_canvas_elements", {"limit": 1}, snapshot, {}, tmp_path)
    assert page["elements"][0]["id"] == "text" and page["nextOffset"] == 1 and page["truncated"]
    filtered = execute_tool("list_canvas_elements", {"kind": "image"}, snapshot, {}, tmp_path)
    assert filtered["total"] == 1 and filtered["nextOffset"] is None
    objects = execute_tool("get_canvas_elements", {"objectIds": ["text"]}, snapshot, {}, tmp_path)
    assert objects["elements"][0]["text"] == "轻盈出发"


def test_image_observation_comes_from_bytes_not_canvas_dimensions(tmp_path):
    snapshot = project_snapshot(canvas(), [])
    bindings = bind_image(tmp_path)
    result = execute_tool("inspect_image", {"assetId": "asset"}, snapshot, bindings, tmp_path)
    assert (result["width"], result["height"], result["format"]) == (17, 23, "PNG")
    assert result["sha256"] == bindings["asset"]["sha256"]
    assert result["bytes"] == bindings["asset"]["bytes"]
    assert "storage_key" not in result


@pytest.mark.parametrize("name,args,code", [
    ("write_canvas", {}, "UNKNOWN_TOOL"),
    ("read_canvas", {"url": "https://example.com"}, "INVALID_TOOL_ARGUMENTS"),
    ("list_canvas_elements", {"limit": "1"}, "INVALID_TOOL_ARGUMENTS"),
    ("get_canvas_elements", {"objectIds": ["foreign"]}, "OBJECT_OUT_OF_SCOPE"),
    ("inspect_image", {"assetId": "foreign"}, "ASSET_OUT_OF_SCOPE"),
])
def test_tool_boundaries_reject_unbound_reads_and_invalid_inputs(tmp_path, name, args, code):
    with pytest.raises(ToolError) as caught:
        execute_tool(name, args, project_snapshot(canvas(), []), {}, tmp_path)
    assert caught.value.code == code


def test_file_change_and_path_escape_cannot_become_evidence(tmp_path):
    snapshot, binding = project_snapshot(canvas(), []), bind_image(tmp_path)
    (tmp_path / "actual.png").write_bytes(b"changed")
    with pytest.raises(ToolError) as changed:
        execute_tool("inspect_image", {"assetId": "asset"}, snapshot, binding, tmp_path)
    assert changed.value.code == "ASSET_CHANGED"
    binding["asset"]["storage_key"] = "../outside.png"
    with pytest.raises(ToolError) as escaped:
        execute_tool("inspect_image", {"assetId": "asset"}, snapshot, binding, tmp_path)
    assert escaped.value.code == "ASSET_OUT_OF_SCOPE"


def test_output_limit_and_missing_selection_are_explicit(tmp_path):
    with pytest.raises(ToolError) as missing:
        project_snapshot(canvas(), ["deleted"])
    assert missing.value.code == "SELECTION_CHANGED"
    with pytest.raises(ToolError) as limited:
        execute_tool("get_canvas_elements", {"objectIds": ["text"]}, project_snapshot(canvas(), []),
                     {}, tmp_path, max_output_bytes=20)
    assert limited.value.code == "TOOL_OUTPUT_LIMIT"
    schemas = tool_schemas()
    assert len(schemas) == 4
    assert all(schema["function"]["parameters"]["additionalProperties"] is False for schema in schemas)


def test_saved_generation_and_internal_media_url_resolve_real_asset_identity(tmp_path):
    asset_id = '12345678-1234-1234-1234-123456789012'
    raw = canvas()
    raw['objects']['image'] = {'kind': 'placeholder', 'generationId': 'generation-1',
        'x': 0, 'y': 0, 'width': 320, 'height': 320}
    raw['generations'] = {'generation-1': {'status': 'completed', 'result': {
        'kind': 'image', 'assetId': None, 'src': f'/api/assets/{asset_id}/content', 'ext': 'png'},
        'spec': {'maskAssetId': 'must-not-copy'}}}
    snapshot = project_snapshot(raw, [])
    assert snapshot['objects']['image']['kind'] == 'image'
    assert snapshot['objects']['image']['assetId'] == asset_id
    assert 'must-not-copy' not in json.dumps(snapshot)
    image_binding = bind_image(tmp_path)['asset']
    result = execute_tool('inspect_image', {'assetId': asset_id}, snapshot, {asset_id: image_binding}, tmp_path)
    assert (result['width'], result['height']) == (17, 23)
    raw['generations']['generation-1']['result']['src'] = f'https://foreign.test/api/assets/{asset_id}/content'
    assert 'assetId' not in project_snapshot(raw, [])['objects']['image']
