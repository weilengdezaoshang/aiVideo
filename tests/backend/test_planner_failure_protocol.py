"""Planner failure protocol. Fake providers only; no paid model and no supervisor engine."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

from backend.agent.providers import ModelCallError, _parse_response
from backend.core.supervisor.planners import CascadePlanner, LLMPlanner, PlannerUnknown
from tests.backend.test_planner_cascade import _Provider, _route, _tool_context


def _call(name, arguments, ident="a", usage=None, extra=None):
    body = {"id": "resp", "model": "deployment-model", "choices": [{"index": 0, "message": {
        "role": "assistant", "content": None, "tool_calls": [{"id": ident, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]},
        "finish_reason": "tool_calls"}], "usage": usage or {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
    if extra:
        body.update(extra)
    return body


def _text(content, usage=None):
    return {"id": "resp", "choices": [{"index": 0, "message": {
        "role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": usage or {"total_tokens": 6}}


class _SilentProvider:
    """Returns a parsed response and never calls response_sink."""

    def __init__(self, raw):
        self.calls = 0
        self.raw = raw

    async def complete(self, route, messages, tools, response_sink=None):
        self.calls += 1
        assert response_sink is not None
        return _parse_response(self.raw, None)


class _Rows:
    def __init__(self):
        self.reserved = False
        self.row = None
        self.drop = False

    def begin(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt):
        params = stmt.compile().params
        if not self.reserved:
            self.reserved = True
            self.row = SimpleNamespace(
                request_hash=params.get("request_hash"), raw_response=None, usage={},
                status="sending", finished_at=None)
            return SimpleNamespace(scalar_one_or_none=lambda: 1)
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def scalar(self, _stmt):
        if self.drop:
            return None
        return self.row


def _assert_rejected(result, code):
    assert result.plan.steps == []
    assert result.plan.result_type == "answer_only"
    assert result.plan.writer == "llm"
    assert result.plan.answer == ""
    assert result.raw_output["rejected_planning_result"] is True
    assert result.raw_output["writer"] == "llm"
    assert result.raw_output["cascade_layer"] == "llm"
    assert result.raw_output["conversion_errors"][0]["code"] == code
    assert result.raw_output["conversion_errors"][0]["detail"]
    blob = json.dumps(result.raw_output, ensure_ascii=False)
    assert "reasoning" not in blob
    assert "supersecret" not in blob
    assert "sk-live" not in blob


def test_large_raw_without_sink_is_stored_before_parse_and_reused():
    note = "n" * 9000
    raw = _call("answer_only", {"answer": "好" + note}, extra={
        "reasoning_content": "private-chain-of-thought",
        "debug": {"authorization": "Bearer supersecret-token"}})
    assert len(json.dumps(raw)) > 8000
    rows = _Rows()
    provider = _SilentProvider(raw)
    planner = LLMPlanner(provider, _route(), rows)
    context = _tool_context("你好")
    context["invocation_key"] = str(uuid.uuid4())
    seen = {}

    import backend.agent.providers as providers
    real = providers._parse_response

    def wrapped(payload, request_id):
        seen["stored_first"] = rows.row.raw_response is not None
        return real(payload, request_id)

    providers._parse_response = wrapped
    try:
        result = asyncio.run(planner.plan(context))
    finally:
        providers._parse_response = real
    assert seen["stored_first"] is True
    assert provider.calls == 1
    assert result.plan.answer.startswith("好")
    assert result.tokens == 5
    stored = json.dumps(rows.row.raw_response, ensure_ascii=False)
    assert len(stored) > 8000
    assert note[:100] in stored
    assert "private-chain-of-thought" not in stored
    assert "supersecret" not in stored
    assert rows.row.status == "stored"
    again = asyncio.run(planner.plan(dict(context)))
    assert provider.calls == 1
    assert again.plan.answer == result.plan.answer


def test_missing_model_call_row_raises_without_leaking_payload():
    rows = _Rows()
    rows.drop = True
    raw = _call("answer_only", {"answer": "Bearer supersecret-token sk-live-secret"})
    provider = _SilentProvider(raw)
    planner = LLMPlanner(provider, _route(), rows)
    context = _tool_context("你好")
    ident = uuid.uuid4()
    context["invocation_key"] = str(ident)
    try:
        asyncio.run(planner.plan(context))
        raise AssertionError("missing row must not succeed")
    except PlannerUnknown as exc:
        text = str(exc)
    assert f"planner_model_call_missing:{ident}" in text
    assert "supersecret" not in text
    assert "sk-live" not in text
    assert provider.calls == 1


def test_hash_mismatch_refuses_a_second_network_call():
    rows = _Rows()
    provider = _SilentProvider(_call("answer_only", {"answer": "好"}))
    planner = LLMPlanner(provider, _route(), rows)
    context = _tool_context("你好")
    context["invocation_key"] = str(uuid.uuid4())
    asyncio.run(planner.plan(context))
    rows.row.request_hash = "0" * 64
    try:
        asyncio.run(planner.plan(dict(context)))
        raise AssertionError("hash mismatch must refuse")
    except PlannerUnknown as exc:
        assert "hash mismatch" in str(exc)
    assert provider.calls == 1


def test_invalid_conversion_is_durable_and_recovered_without_network():
    cases = [
        (_call("not_a_real_tool", {"prompt": "Bearer supersecret-token"}), "unknown_tool"),
        (_call("answer_only", {"answer": 1, "apiKey": "sk-live-secret"}), "parameter_schema_invalid"),
        (_text("只是文字，没有工具。reasoning: private"), "empty_plan"),
    ]
    for raw, code in cases:
        rows = _Rows()
        provider = _Provider(raw)
        planner = LLMPlanner(provider, _route(), rows)
        context = _tool_context("把这个改一下")
        context["invocation_key"] = str(uuid.uuid4())
        result = asyncio.run(planner.plan(context))
        _assert_rejected(result, code)
        assert result.tokens == raw["usage"]["total_tokens"]
        assert result.raw_output["usage"]["total_tokens"] == result.tokens
        assert rows.row.raw_response is not None
        assert provider.calls == 1
        again = asyncio.run(planner.plan(dict(context)))
        assert provider.calls == 1
        assert again.raw_output["conversion_errors"] == result.raw_output["conversion_errors"]
        assert again.tokens == result.tokens
        names = [step.tool for step in again.plan.steps]
        assert "not_a_real_tool" not in names
        assert names == []


def test_blocked_keeps_usage_and_does_not_switch_or_retry():
    usage = {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}
    error = ModelCallError("MODEL_CONTENT_BLOCKED", "blocked raw reasoning sk-live", usage=usage,
                            raw_response={"usage": usage, "reasoning_content": "private-thought"})
    provider = _Provider(error=error)
    cascade = CascadePlanner(LLMPlanner(provider, _route()))
    result = asyncio.run(cascade.plan(_tool_context("随便聊聊")))
    assert provider.calls == 1
    assert result.plan.steps == []
    assert result.plan.result_type == "answer_only"
    assert result.raw_output["planner_failure"] == {"code": "planner_blocked", "status": "blocked"}
    assert result.raw_output["cascade_layer"] == "rule_blocked"
    assert result.tokens == 12
    assert result.raw_output["usage"]["total_tokens"] == 12
    assert "private-thought" not in json.dumps(result.raw_output)
    assert "sk-live" not in result.plan.answer
    english = asyncio.run(cascade.plan(_tool_context("随便聊聊", language="en-US")))
    assert provider.calls == 2
    assert "cannot be generated" in english.plan.answer.lower()
    assert english.tokens == 12


def test_timeout_marks_unknown_usage_and_rule_still_skips_the_model():
    timeout = _Provider(error=ModelCallError("MODEL_RESULT_UNKNOWN", "timeout", unknown=True))
    cascade = CascadePlanner(LLMPlanner(timeout, _route()))
    result = asyncio.run(cascade.plan(_tool_context("把这个改一下", language="en")))
    assert timeout.calls == 1
    assert result.raw_output["planner_failure"]["status"] == "unknown"
    assert result.raw_output["usage"] == {"unknown": True}
    assert result.tokens == 0
    assert "unknown" in result.plan.answer.lower()
    rule = _tool_context("抠图", selected=["image-1"])
    owned = asyncio.run(cascade.plan(rule))
    assert timeout.calls == 1
    assert owned.plan.writer == "rule"
    assert owned.raw_output["cascade_layer"] == "rule"
    assert "planner_failure" not in owned.raw_output
    assert [step.tool for step in owned.plan.steps] == ["detect_subject", "cutout"]


def test_invalid_response_with_usage_is_unavailable_and_keeps_tokens():
    usage = {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
    provider = _Provider(error=ModelCallError(
        "INVALID_MODEL_RESPONSE", "bad", usage=usage, raw_response={"usage": usage}))
    result = asyncio.run(CascadePlanner(LLMPlanner(provider, _route())).plan(_tool_context("你好呀")))
    assert provider.calls == 1
    assert result.raw_output["planner_failure"]["status"] == "unavailable"
    assert result.tokens == 4
    assert result.plan.steps == []
    zh = asyncio.run(CascadePlanner(None).plan(_tool_context("你好呀")))
    assert "暂不可用" in zh.plan.answer and zh.plan.steps == []
    en = asyncio.run(CascadePlanner(None).plan(_tool_context("你好呀", language="en-GB")))
    assert "temporarily unavailable" in en.plan.answer.lower()
