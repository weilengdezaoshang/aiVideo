"""Host admission gates run without infrastructure or provider calls."""
import pytest
from pydantic import ValidationError
from backend.agent.media.catalog import Catalog, HostError
from backend.agent.media.contracts import CatalogConfig, HostTurn
from backend.agent.media.planner import plan_intent, validate_plan


def config(**overrides):
    raw = {"version": "unit-v1", "environment": "development", "enabled": True,
           "capabilities": [{"id": "text_to_image", "enabled": True, "outputType": "image",
             "tiers": {"preview": {"credits": 10}}, "binding": {"version": "unit-v1",
             "providerSnapshot": {"provider": "mock", "configVersion": "unit-v1", "cloudModel": "mock"}}}]}
    return CatalogConfig.model_validate({**raw, **overrides})


def test_production_mock_and_missing_reviewer_fail_closed():
    with pytest.raises(ValueError, match="prod_mock"):
        Catalog(config(environment="production"))
    with pytest.raises(ValueError, match="vision route"):
        Catalog(config(reviewMode="hard"))
    with pytest.raises(HostError, match="不可用"):
        Catalog.load(None).resolve("text_to_image", "preview")


def test_browser_cannot_override_binding_or_skip_revision():
    raw = {"clientRequestId": "id", "documentId": "doc", "message": "生成图片", "baseRevision": 1}
    for injected in ({"modelId": "paid-model"}, {"providerSnapshot": {}}, {"baseRevision": "1"}):
        with pytest.raises(ValidationError):
            HostTurn.model_validate({**raw, **injected})


def test_cycle_contention_and_video_degradation_are_rejected():
    catalog = Catalog(config())
    plan = plan_intent("生成四方向图片", [], catalog)
    assert not validate_plan(plan, catalog)
    first = plan.steps[0]
    other = first.model_copy(update={"id": "other"})
    conflict = plan.model_copy(update={"steps": [first, other]})
    assert "plan_node_contention" in {x.code for x in validate_plan(conflict, catalog)}
    cycle = plan.model_copy(update={"steps": [first.model_copy(update={"dependsOn": ["other"]}),
                         other.model_copy(update={"dependsOn": [first.id]})]})
    assert validate_plan(cycle, catalog)[0].code == "plan_cycle"
    assert "plan_video_degraded_to_image" in {x.code for x in validate_plan(plan.model_copy(update={"goalType": "video"}), catalog)}


def test_unavailable_video_and_unsupported_specs_never_become_images():
    catalog = Catalog(config())
    for message in ("生成视频", "做一个四秒广告"):
        with pytest.raises(HostError):
            plan_intent(message, [], catalog)
    with pytest.raises(HostError, match="参考图"):
        plan_intent("这张图做视频", [], catalog)
    with pytest.raises(HostError):
        plan_intent("生成4K图片", [], catalog)
    public = str(catalog.public())
    assert "providerSnapshot" not in public and "mock" not in public


def test_examples_are_valid_and_disabled_by_default():
    from pathlib import Path
    for path in Path("deploy").glob("host-catalog.*.json"):
        assert not Catalog.load(path).config.enabled
