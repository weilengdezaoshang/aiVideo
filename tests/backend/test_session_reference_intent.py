import pytest

from backend.core.session_reference_intent import reference_intent


@pytest.mark.parametrize(("text", "expected"), [
    ("把刚才那张背景换掉", {"kind": "latest"}),
    ("把第二张改成蓝色", {"kind": "ordinal", "ordinal": 2, "batch_id": "batch"}),
    ("第十二张更暗一点", {"kind": "ordinal", "ordinal": 12, "batch_id": "batch"}),
    ("第20张", {"kind": "ordinal", "ordinal": 20, "batch_id": "batch"}),
    ("左边那个抠图", {"kind": "spatial", "spatial": "left"}),
    ("右侧那张再来一次", {"kind": "spatial", "spatial": "right"}),
    ("编辑选中的图片", {"kind": "selected"}),
])
def test_explicit_reference(text, expected):
    assert reference_intent(text, session_id="session", batch_id="batch") == {
        "caller_session_id": "session", **expected}


def test_conflicting_references_ask_instead_of_guessing():
    with pytest.raises(ValueError, match="ambiguous_reference_intent"):
        reference_intent("左边那个和右边那个", session_id="session")
    assert reference_intent("生成一只兔子", session_id="session") is None
