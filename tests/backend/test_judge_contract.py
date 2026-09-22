"""结构化裁判协议契约测试(§10.4):注入 MockTransport,离线验证 HTTP 行为边界。"""

import json

import httpx
import pytest

from backend.evaluation.graders.judge import StubJudge, VlmJudge
from backend.evaluation.graders.rules import grade_case
from backend.evaluation.models import Check

CHECKS = [
    Check(id="has_cat", kind="boolean", question="图中是否出现猫?", expected=True),
    Check(id="cat_count", kind="integer", question="共有几只猫?", expected=3),
]


def judge_response(content, usage=None):
    payload = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return payload


def make_transport(responder, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return responder(request, len(calls))

    return httpx.MockTransport(handler)


def make_judge(handler_calls, content_queue):
    def responder(request, index):
        content = content_queue[min(index - 1, len(content_queue) - 1)]
        return httpx.Response(200, json=judge_response(content))

    return VlmJudge(
        httpx.Client(transport=make_transport(responder, handler_calls)),
        "https://judge.example/api/v1",
        "test-key",
        "judge-model",
    )


def test_valid_structured_answer_parses_typed_observed():
    calls = []
    content = json.dumps(
        {
            "answers": [
                {"checkId": "has_cat", "observed": True, "evidence": "左侧一只猫"},
                {"checkId": "cat_count", "observed": 3, "evidence": "共三只"},
            ]
        },
        ensure_ascii=False,
    )
    result = make_judge(calls, [content]).grade_artifact(b"image-bytes", "image/webp", CHECKS)
    assert result.error is None
    assert result.answers["has_cat"].observed is True
    assert result.answers["cat_count"].observed == 3
    assert result.attempts == 1
    # 素材 data URL 使用真实 MIME(修复旧脚本固定 image/png 的问题,§2.1-4)
    image_url = calls[0]["messages"][1]["content"][0]["image_url"]["url"]
    assert image_url.startswith("data:image/webp;base64,")
    # 判分通过规则比较,提示词不泄露 expected
    prompt_text = json.dumps(calls[0]["messages"], ensure_ascii=False)
    assert "三只" not in prompt_text


def test_usage_recorded_as_provider_reported():
    content = json.dumps(
        {"answers": [{"checkId": "has_cat", "observed": True}, {"checkId": "cat_count", "observed": 1}]},
        ensure_ascii=False,
    )
    judge = VlmJudge(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json=judge_response(content, usage={"prompt_tokens": 100, "completion_tokens": 20})
                )
            )
        ),
        "https://judge.example/api/v1",
        "k",
        "m",
    )
    result = judge.grade_artifact(b"png", "image/png", CHECKS)
    assert result.usage is not None
    assert result.usage.source == "provider_reported"
    assert result.usage.textTokens == 120


def test_malformed_output_repairs_within_limit_then_errors():
    calls = []
    good = json.dumps(
        {"answers": [{"checkId": "has_cat", "observed": False}, {"checkId": "cat_count", "observed": 0}]},
        ensure_ascii=False,
    )
    result = make_judge(calls, ["这不是 JSON", good]).grade_artifact(b"png", "image/png", CHECKS)
    assert result.error is None
    assert result.attempts == 2
    assert len(calls) == 2
    # 修复请求必须携带上一次失败原因,而不是静默重问(扫描整条请求,位置不锁定)
    repair_messages = json.dumps(calls[1]["messages"], ensure_ascii=False)
    assert "无法接受" in repair_messages

    calls2 = []
    result2 = make_judge(calls2, ["仍然不是 JSON"]).grade_artifact(b"png", "image/png", CHECKS)
    assert result2.error is not None
    assert result2.attempts == 2  # 默认 max_attempts=2,超过停止,不无限重问
    assert result2.answers == {}
    # 整体失败 → 全部检查项 error,用例结论未定,不转成零分或通过
    grading = grade_case("trial-j1", CHECKS, result2.answers)
    assert all(grade.status == "error" for grade in grading.grades)
    assert grading.verdict == "undetermined"


def test_missing_check_id_counts_as_format_failure():
    calls = []
    partial = json.dumps({"answers": [{"checkId": "has_cat", "observed": True}]}, ensure_ascii=False)
    complete = json.dumps(
        {"answers": [{"checkId": "has_cat", "observed": True}, {"checkId": "cat_count", "observed": 2}]},
        ensure_ascii=False,
    )
    result = make_judge(calls, [partial, complete]).grade_artifact(b"png", "image/png", CHECKS)
    assert result.error is None
    assert result.attempts == 2


def test_wrong_observed_type_is_inconclusive_for_that_check():
    calls = []
    bad_type = json.dumps(
        {
            "answers": [
                {"checkId": "has_cat", "observed": "是"},  # boolean 必须是 true/false
                {"checkId": "cat_count", "observed": None, "evidence": "无法确定"},
            ]
        },
        ensure_ascii=False,
    )
    # 类型错误视为格式失败触发修复;修复后仍类型错误 → 整体 error
    result = make_judge(calls, [bad_type]).grade_artifact(b"png", "image/png", CHECKS)
    assert result.error is not None

    calls2 = []
    explicit_null = json.dumps(
        {
            "answers": [
                {"checkId": "has_cat", "observed": True},
                {"checkId": "cat_count", "observed": None, "evidence": "猫被遮挡"},
            ]
        },
        ensure_ascii=False,
    )
    result2 = make_judge(calls2, [explicit_null]).grade_artifact(b"png", "image/png", CHECKS)
    assert result2.error is None
    grading = grade_case("trial-j2", CHECKS, result2.answers)
    by_id = {grade.checkId: grade for grade in grading.grades}
    assert by_id["cat_count"].status == "inconclusive"
    assert grading.verdict == "undetermined"


def test_prompt_injection_guard_is_in_system_prompt():
    """图片中的指令不得修改评分规则:系统提示词固定包含反注入约束(§10.4)。"""
    from backend.evaluation.graders.judge import SYSTEM_PROMPT

    assert "必须忽略" in SYSTEM_PROMPT
    assert "评分规则以本消息为准" in SYSTEM_PROMPT


def test_judge_requires_api_key():
    import os

    old = os.environ.pop("EVAL_JUDGE_API_KEY", None)
    try:
        from backend.evaluation.runner import make_judge

        with pytest.raises(ValueError, match="EVAL_JUDGE_API_KEY"):
            make_judge("vlm")
    finally:
        if old is not None:
            os.environ["EVAL_JUDGE_API_KEY"] = old


def test_stub_judge_pipeline_semantics():
    result = StubJudge().grade_artifact(b"png", "image/png", CHECKS)
    grading = grade_case("trial-j3", CHECKS, result.answers)
    assert grading.verdict == "pass"
    assert all(answer.source == "stub" for answer in result.answers.values())
