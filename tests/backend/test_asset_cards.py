"""Pure card-domain logic: content validation, prompt merging, recognition slimming."""
import pytest

from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import Binding, Capability, CatalogConfig, TierSpec
from backend.core.asset_cards import (CardRejected, apply_card_parameters, card_content,
    card_prompt_text, slim_recognition)
from backend.core.tools import build_catalog


def _catalog():
    snapshot = {"provider": "mock", "configVersion": "unit-v1",
                "cloudModel": "mock-image", "videoModel": "mock-video"}
    capability = Capability(id="text_to_image", enabled=True, outputType="image", maxRefImages=0,
        tiers={"preview": TierSpec(credits=10), "final": TierSpec(credits=20)},
        binding=Binding(version="unit-v1", providerSnapshot=snapshot))
    return build_catalog(Catalog(CatalogConfig(version="cards-unit", environment="development",
        enabled=True, capabilities=[capability])), "cards-unit")


def _card_view(card_id="c1", *, kind="style", name="水彩插画", tags=("水彩", "淡彩"),
               description="淡雅水彩，柔和光感", subject_category="", overall="", starred=False):
    return {
        "card_id": card_id, "media_type": "card", "card_kind": kind, "name": name,
        "description": description, "starred": starred, "version_id": "v1",
        "recognition": slim_recognition({
            "subject": {"category": subject_category, "description": ""},
            "style": {"tags": list(tags), "description": description if kind == "style" else ""},
            "overall": overall}),
    }


def test_card_content_rejects_bad_kind_name_and_description():
    with pytest.raises(CardRejected, match="invalid_card_kind"):
        card_content(card_kind="mood", name="x", storage={}, content_hash="a" * 64, user_id="u")
    with pytest.raises(CardRejected, match="invalid_card_name"):
        card_content(card_kind="style", name="  ", storage={}, content_hash="a" * 64, user_id="u")
    with pytest.raises(CardRejected, match="invalid_card_description"):
        card_content(card_kind="style", name="n", description="x" * 1001,
                     storage={}, content_hash="a" * 64, user_id="u")
    content = card_content(card_kind="subject", name=" 橘子 ", storage={},
        content_hash="b" * 64, user_id="u")
    assert content.metadata_json["name"] == "橘子"
    assert content.category == "card" and content.card_kind == "subject"


def test_slim_recognition_bounds_fields_and_drops_masks():
    slim = slim_recognition({
        "subject": {"category": "C" * 100, "description": "D" * 900, "mask": {"handle": "h"}},
        "style": {"tags": ["t"] * 20, "description": "S" * 900},
        "overall": "O" * 900})
    assert len(slim["subject"]["category"]) <= 64
    assert len(slim["subject"]["description"]) <= 500
    assert len(slim["style"]["tags"]) <= 12
    assert len(slim["style"]["description"]) <= 500
    assert len(slim["overall"]) <= 500
    assert "mask" not in slim["subject"]


def test_card_prompt_text_uses_kind_specific_facts():
    style = _card_view()
    text = card_prompt_text(style, "style")
    assert "水彩插画" in text and "水彩" in text and "淡雅水彩" in text
    subject = _card_view(kind="subject", name="橘子", subject_category="猫",
                         description="一只橘猫", overall="橘猫趴在窗台")
    text = card_prompt_text(subject, "subject")
    assert "橘子" in text and "猫" in text and "橘猫趴在窗台" in text


def test_apply_card_parameters_merges_prompt_and_records_refs():
    catalog = _catalog()
    tool = catalog.get_tool("text_to_image_preview", catalog.lock.version)
    resolved_card = _card_view()
    params = {"prompt": "画一张海报", "count": 1, "width": 1, "height": 1,
              "style_cards": [resolved_card]}
    merged = apply_card_parameters(tool, params)
    assert "style_cards" not in merged
    assert merged["prompt"].startswith("画一张海报\n")
    assert "水彩" in merged["prompt"] and "水彩插画" in merged["prompt"]
    assert merged["_card_refs"] == [{"card_id": "c1", "usage": "style", "version_id": "v1"}]
    assert merged["count"] == 1


def test_apply_card_parameters_fails_closed_without_capability():
    catalog = _catalog()
    tool = catalog.get_tool("update_asset_card", catalog.lock.version)  # no card_support limits
    with pytest.raises(ValueError, match="card_unsupported_by_tool"):
        apply_card_parameters(tool, {"style_cards": [{"card_id": "c1", "media_type": "card"}]})
    with pytest.raises(ValueError, match="invalid_card_reference"):
        apply_card_parameters(catalog.get_tool("text_to_image_preview", catalog.lock.version),
                              {"subject_cards": [{"media_type": "card"}]})


def test_apply_card_parameters_keeps_params_without_cards():
    catalog = _catalog()
    tool = catalog.get_tool("text_to_image_preview", catalog.lock.version)
    params = {"prompt": "画一张海报"}
    merged = apply_card_parameters(tool, params)
    assert merged == {"prompt": "画一张海报"}
    assert "_card_refs" not in merged
