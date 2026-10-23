"""Reply facts come from code and measured read-only results."""
from backend.core.supervisor.replies import failed, opening, read_summary


def test_opening_describes_action_and_reviewed_quote():
    assert "准备执行：换背景" in opening("zh-CN", 12, confirm=False, goal="换背景")
    text = opening("en", 12, confirm=True, goal="Change the background")
    assert "Change the background" in text and "12 credits" in text and "after you confirm" in text


def test_failure_has_a_safe_reason_and_actual_refund():
    assert "参数无效" in failed("zh-CN", 7, "param_invalid")
    assert "7 credits" in failed("en", 7, "provider_down")
    assert "sk-private" not in failed("en", 7, "sk-private")


def test_read_only_completion_uses_measured_data():
    assert read_summary("en", "inspect_image", {"result": {"data": {"width": 640, "height": 480}}}) == "Image dimensions: 640×480 pixels."
    assert read_summary("zh-CN", "read_canvas", {"result": {"data": {"objectCount": 3}}}) == "画布中有 3 个可读对象。"
    assert "note" in read_summary("en", "get_canvas_elements", {"result": {"data": {"elements": [{"text": "note"}]}}})
    assert read_summary("en", "read_canvas", {}) == ""
