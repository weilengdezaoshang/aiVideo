"""Catalog contract for the new asset-card tools."""
from jsonschema import Draft202012Validator

from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.core.tools import build_catalog


def _catalog():
    return build_catalog(Catalog(CatalogConfig(version="catalog-unit", environment="development",
        enabled=True, capabilities=[])), "catalog-unit")


def test_image_analyze_is_a_priced_read_only_tool():
    catalog = _catalog()
    tool = catalog.get_tool("image_analyze", catalog.lock.version)
    assert tool.mode == "read_only"
    assert tool.price.credits == 0  # default; deployments may price it explicitly
    assert tool.execution.adapter == "vision.analyze"
    assert tool.execution.cancellable is True
    assert [port.path for port in tool.inputs] == ["image"]
    assert [port.path for port in tool.outputs] == ["analysis"]
    Draft202012Validator.check_schema(tool.parameters)
    Draft202012Validator.check_schema(tool.result_schema)
    valid = {"image": {"asset_id": "a1", "media_type": "image"}, "question": "这是什么风格"}
    assert Draft202012Validator(tool.parameters).is_valid(valid)
    assert not Draft202012Validator(tool.parameters).is_valid({"image": valid["image"]})


def test_card_tools_are_deterministic_and_zero_priced():
    catalog = _catalog()
    create = catalog.get_tool("create_asset_card", catalog.lock.version)
    update = catalog.get_tool("update_asset_card", catalog.lock.version)
    for tool in (create, update):
        assert tool.mode == "deterministic"
        assert tool.price.credits == 0
        assert tool.execution.adapter.startswith("cards.")
        Draft202012Validator.check_schema(tool.parameters)
    assert set(create.parameters["required"]) == {"card_kind", "name"}
    assert set(update.parameters["required"]) == {"card_id", "expected_version_number"}
    result = create.result_schema
    assert set(result["properties"]) == {"card_id", "media_type", "version_id", "version_number"}
    assert set(result["required"]) == {"card_id", "media_type", "version_id", "version_number"}


def test_card_tools_accept_asset_references_only():
    catalog = _catalog()
    create = catalog.get_tool("create_asset_card", catalog.lock.version)
    literal = create.parameters["properties"]["image"]["anyOf"][0]
    assert literal["properties"]["asset_id"]["type"] == "string"
    assert literal["properties"]["media_type"]["const"] == "image"
    assert any(schema.get("type") == "string" and "$" in schema.get("pattern", "")
               for schema in create.parameters["properties"]["image"]["anyOf"])
    valid = {"card_kind": "style", "name": "水彩", "image": {"asset_id": "a1", "media_type": "image"}}
    assert Draft202012Validator(create.parameters).is_valid(valid)
    assert not Draft202012Validator(create.parameters).is_valid(
        {"card_kind": "mood", "name": "x"})


def test_generation_card_support_recorded_on_bound_tools():
    from backend.agent.media.contracts import Binding, Capability, TierSpec
    snapshot = {"provider": "mock", "configVersion": "unit-v1",
                "cloudModel": "mock-image", "videoModel": "mock-video"}
    capability = Capability(id="text_to_image", enabled=True, outputType="image", maxRefImages=0,
        tiers={"preview": TierSpec(credits=10)},
        binding=Binding(version="unit-v1", providerSnapshot=snapshot))
    bound = build_catalog(Catalog(CatalogConfig(version="bound-unit", environment="development",
        enabled=True, capabilities=[capability])), "bound-unit")
    tool = bound.get_tool("text_to_image_preview", bound.lock.version)
    assert tool.available is True
    assert tool.limits["card_support"] == {"prompt_text": True, "image_input": False,
                                           "multi_reference": False}
    assert "style_cards" in tool.parameters["properties"]
    assert "style_cards" not in tool.parameters.get("required", [])
    # Unbound placeholders stay unavailable without card claims.
    placeholder = _catalog().get_tool("text_to_image_preview", _catalog().lock.version)
    assert placeholder.available is False
    assert "card_support" not in (placeholder.limits or {})
