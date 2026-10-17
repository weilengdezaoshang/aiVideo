import pytest

from backend.agent.media.canvas_actions import is_cutout_request, prepare_cutout, validate_cutout_result
from backend.agent.media.catalog import HostError

OBJECTS = {"image": {"kind": "image", "assetId": "source"}, "video": {"kind": "video", "assetId": "clip"}}


@pytest.mark.parametrize("message", ["帮这张图抠图", "提取主体", "移除背景", "把人物抠出来", "去背"])
def test_cutout_routes_to_explicit_canvas_confirmation(message):
    assert is_cutout_request(message)
    assert prepare_cutout(message, ["image"], OBJECTS) == {
        "type": "cutout", "sourceObjectId": "image", "sourceAssetId": "source"}


@pytest.mark.parametrize("message", ["不要抠图，生成一张海报", "不用去背", "生成风景图片"])
def test_unrelated_and_negated_requests_are_not_canvas_handoffs(message):
    assert not is_cutout_request(message)


@pytest.mark.parametrize("selected", [[], ["video"], ["image", "video"], ["missing"]])
def test_ambiguous_or_unavailable_sources_require_clarification(selected):
    with pytest.raises(HostError) as exc:
        prepare_cutout("抠图", selected, OBJECTS)
    assert exc.value.code == "intent_clarify"


def test_cutout_and_video_does_not_silently_drop_the_video_goal():
    with pytest.raises(HostError):
        prepare_cutout("抠图后制作视频", ["image"], OBJECTS)


def test_result_must_be_a_saved_cutout_of_the_original_asset():
    action = prepare_cutout("抠图", ["image"], OBJECTS)
    result = {"kind": "image", "assetId": "result", "lineage": {
        "fromId": "image", "params": {"operation": "cutout"}}, "cutout": {"originalAssetId": "source"}}
    assert validate_cutout_result(action, "result-node", {"result-node": result}) == "result"
    for patch in ({"kind": "video"}, {"cutout": {"originalAssetId": "other"}}, {"lineage": {"fromId": "other"}}):
        with pytest.raises(HostError):
            validate_cutout_result(action, "result-node", {"result-node": {**result, **patch}})
