"""Definition/export/price tests: providers are never called."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError
from pydantic import ValidationError as ModelValidationError

from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.core.tools import (CatalogError, CatalogSnapshot, ToolCatalog, ToolSettings,
                                VersionLock, build_catalog, estimate_cost)
from backend.core.tools.contracts import Price


def catalog(version="tools-v1"):
    raw = json.loads(Path("deploy/host-catalog.development.json").read_text())
    raw["enabled"] = True
    for cap in raw["capabilities"]:
        cap["enabled"] = True
    return build_catalog(Catalog(CatalogConfig.model_validate(raw)), version)


def test_all_existing_capabilities_have_definitions_and_no_membership_fields():
    c = catalog()
    names = {t.name for t in c.snapshot.tools}
    assert {"text_to_image_preview", "image_edit_final", "inpaint_preview", "text_to_video_final",
            "image_to_video_preview", "frames_to_video_preview", "detect_subject", "cutout",
            "video_concat", "upscale", "outpaint", "read_canvas", "inspect_image"} <= names
    encoded = json.dumps(c.snapshot.model_dump())
    assert not any(word in encoded for word in ("membership", "member_level", "minimum_tier"))
    for tool in c.snapshot.tools:
        Draft202012Validator.check_schema(tool.parameters)
        Draft202012Validator.check_schema(tool.result_schema)
    assert not c.get_tool("upscale", c.lock.version).available
    assert not c.get_tool("outpaint", c.lock.version).available
    assert not c.get_tool("inpaint_preview", c.lock.version).available  # Mock has no mask contract.


def test_exports_share_locked_definition_and_no_secrets_or_execution_handles():
    c = catalog()
    rules = c.to_validator_rules(c.lock.version)
    models = c.to_model_tools({}, c.lock)
    for entry in models:
        assert entry["type"] == "function"
        function = entry["function"]
        tool = c.get_tool(function["name"], c.lock.version)
        assert function["parameters"] == rules["tools"][tool.name]["parameters"]
        assert "adapter" not in function and "binding" not in function
        worker = c.to_worker_config(tool.name, c.lock)
        assert worker["version_lock"] == rules["version_lock"]
        assert worker["price"] == rules["tools"][tool.name]["price"]
    assert all("upscale" != entry["function"]["name"] for entry in models)


def test_only_rollout_group_changes_visibility_not_identity_or_credits():
    c = catalog()
    snapshot = c.snapshot
    tools = [t.model_copy(update={"rollout_groups": ["beta"]}) if t.name == "cutout" else t for t in snapshot.tools]
    c = ToolCatalog(CatalogSnapshot(version="gray", tools=tools))
    assert c.to_model_tools({"id": "a", "credits": 0}, c.lock) == c.to_model_tools({"id": "b", "credits": 999}, c.lock)
    assert "cutout" not in {t["function"]["name"] for t in c.to_model_tools({}, c.lock)}
    assert "cutout" in {t["function"]["name"] for t in c.to_model_tools({"rollout_group": "beta"}, c.lock)}


def test_published_snapshot_is_defensively_copied():
    c = catalog()
    lock = c.lock
    c.snapshot.tools[0].parameters.clear()
    c.get_tool("cutout", lock.version).execution.binding["bad"] = True
    assert c.lock == lock
    assert c.get_tool("cutout", lock.version).parameters
    assert not c.get_tool("cutout", lock.version).execution.binding


def test_version_mismatch_and_missing_tool_fail_closed():
    c = catalog()
    with pytest.raises(CatalogError):
        c.to_model_tools({}, VersionLock(version="v2", content_hash=c.lock.content_hash))
    with pytest.raises(CatalogError):
        c.to_model_tools({}, VersionLock(version=c.lock.version, content_hash="0" * 64))
    with pytest.raises(CatalogError):
        c.get_tool("cutout", "latest")
    with pytest.raises(CatalogError):
        c.get_tool("unknown", c.lock.version)
    with pytest.raises(CatalogError):
        c.to_validator_rules("latest")
    with pytest.raises(CatalogError):
        c.to_worker_config("upscale", c.lock)


def test_cost_uses_actual_pinned_tier_and_rejects_bad_parameters():
    c = catalog()
    for tier in ("preview", "final"):
        tool = c.get_tool("text_to_image_" + tier, c.lock.version)
        params = {"prompt": "测试", "count": 3, "width": tool.limits["width"], "height": tool.limits["height"]}
        assert estimate_cost(tool, params) == tool.execution.binding["spec"]["credits"] * 3
        with pytest.raises(ValidationError):
            estimate_cost(tool, {**params, "width": 7})
        with pytest.raises(ValidationError):
            estimate_cost(tool, {**params, "count": True})
    detect = c.get_tool("detect_subject", c.lock.version)
    assert estimate_cost(detect, {"image": {"asset_id": "source", "media_type": "image"}}) == 0
    with pytest.raises(CatalogError):
        estimate_cost(c.get_tool("upscale", c.lock.version), {"image": {"asset_id": "a", "media_type": "image"}})


def test_invalid_or_unpriced_definition_cannot_be_enabled():
    c = catalog()
    definition = c.get_tool("cutout", c.lock.version).model_dump()
    with pytest.raises(ModelValidationError):
        type(c.snapshot.tools[0]).model_validate({**definition, "membership": "vip"})
    with pytest.raises(ModelValidationError):
        type(c.snapshot.tools[0]).model_validate({**definition, "price": {"kind": "unpriced"}})
    with pytest.raises(ModelValidationError):
        Price(kind="per_count", credits=1)


def test_config_limits_and_explicit_export_price():
    c = build_catalog(Catalog.load(None), "limits", ToolSettings(max_candidates=2, max_count=3, export_credits=7))
    assert c.get_tool("detect_subject", "limits").limits["max_candidates"] == 2
    assert estimate_cost(c.get_tool("video_concat", "limits"), {"clips": [{"asset_id": "v", "media_type": "video"}]}) == 7


def test_comfy_mask_binding_can_be_exported_but_cannot_implicitly_switch_cloud_model():
    raw = json.loads(Path("deploy/host-catalog.example.json").read_text())
    raw["enabled"] = True
    for cap in raw["capabilities"]:
        cap["enabled"] = True
    c = build_catalog(Catalog(CatalogConfig.model_validate(raw)), "comfy")
    assert c.get_tool("inpaint_preview", "comfy").available
    definition = c.get_tool("inpaint_preview", "comfy")
    assert definition.execution.binding["requires_mask"]
    assert any(port.media_type == "mask" for port in definition.inputs)


def test_definition_rejects_nested_raw_credentials():
    c = catalog()
    tool = c.get_tool("cutout", c.lock.version)
    value = tool.model_dump()
    value["execution"]["binding"] = {"config": {"apiKey": "do-not-store"}}
    with pytest.raises(ModelValidationError, match="credential references"):
        type(tool).model_validate(value)


def test_rollout_configuration_and_generic_asset_metadata_definition():
    c = build_catalog(Catalog.load(None), "gray-settings", ToolSettings(rollout_groups={"cutout": ["beta"]}))
    assert c.get_tool("read_asset", c.lock.version).mode == "read_only"
    assert "cutout" not in {t["function"]["name"] for t in c.to_model_tools({}, c.lock)}
    assert "cutout" in {t["function"]["name"] for t in c.to_model_tools({"rollout_group": "beta"}, c.lock)}
    with pytest.raises(ValueError, match="unknown tools"):
        build_catalog(Catalog.load(None), "bad-gray", ToolSettings(rollout_groups={"does_not_exist": ["beta"]}))


def test_inventory_is_generated_from_the_same_definitions():
    from backend.core.tools.inventory import inventory_markdown
    c = catalog()
    report = inventory_markdown(c)
    for tool in c.snapshot.tools:
        assert "| " + tool.name + " |" in report
    assert "无固定到期" in report and "未定价" in report
    assert c.get_tool("video_concat", c.lock.version).execution.cancellation == "cooperative"
    assert c.get_tool("frames_to_video_preview", c.lock.version).execution.cancellation == "cooperative"


def test_catalog_reads_do_not_revalidate_all_schemas(monkeypatch):
    c = catalog()
    lock = c.lock
    def repeated_validation(*args, **kwargs):
        raise AssertionError("published schemas must be validated once")
    monkeypatch.setattr(Draft202012Validator, "check_schema", repeated_validation)
    for _ in range(100):
        assert c.get_tool("cutout", lock.version).name == "cutout"
        assert c.lock == lock
    assert c.snapshot.version == lock.version
