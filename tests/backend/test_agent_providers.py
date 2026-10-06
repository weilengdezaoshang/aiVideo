import asyncio
import json

import httpx
import pytest

from backend.agent.config import AgentRoute, load_routes
from backend.agent.providers import ChatCompletionsProvider, ModelCallError


def route(provider="deepseek", **overrides):
    return AgentRoute(id=provider, label=provider, provider=provider, model="deployment-model",
                      baseUrl="https://model.example/v1", credentialRef="agent", **overrides)


def response(content="完成", calls=None, finish="stop"):
    return {"id": "response-1", "model": "deployment-model", "choices": [{"index": 0,
        "message": {"role": "assistant", "content": content, "tool_calls": calls,
                    "reasoning_content": "not retained"}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


@pytest.mark.parametrize("provider", ["deepseek", "bailian"])
def test_adapter_sends_non_thinking_protocol_and_preserves_tool_roundtrip(provider):
    seen = []
    call = {"id": "original-id", "type": "function", "function": {
        "name": "read_canvas", "arguments": "{ }"}}

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers["authorization"] == "Bearer secret"
        assert str(request.url) == "https://model.example/v1/chat/completions"
        assert body["stream"] is False and body["max_tokens"] == 2048
        assert (body.get("thinking") == {"type": "disabled"} if provider == "deepseek"
                else body.get("enable_thinking") is False)
        return httpx.Response(200, json=response(None, [call], "tool_calls") if len(seen) == 1
                              else response(), headers={"x-request-id": "provider-request"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = ChatCompletionsProvider({"agent": {"apiKey": "secret"}}, client)
            messages = [{"role": "user", "content": "画布有什么"}]
            first = await adapter.complete(route(provider), messages, [])
            assert first.tool_calls[0]["arguments"] == {}
            assert first.tool_calls[0]["raw_arguments"] == "{ }"
            assert first.request_id == "provider-request"
            assert first.usage["total_tokens"] == 15
            assert "reasoning_content" not in json.dumps(first.raw_response)
            messages += [first.message(), {"role": "tool", "tool_call_id": "original-id", "content": "{}"}]
            final = await adapter.complete(route(provider), messages, [])
            assert final.content == "完成"
            assert seen[1]["messages"][-2]["tool_calls"] == [call]
    asyncio.run(run())


@pytest.mark.parametrize("code,unknown", [(401, False), (429, False), (503, True), (302, False)])
def test_http_failures_do_not_retry_or_expose_response_secrets(code, unknown):
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(code, text="secret upstream details")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ModelCallError) as caught:
                await ChatCompletionsProvider({"agent": {"apiKey": "secret"}}, client).complete(route(), [], [])
            assert caught.value.unknown is unknown
            assert "secret" not in str(caught.value)
    asyncio.run(run())
    assert len(seen) == 1


@pytest.mark.parametrize("failure,unknown", [(httpx.ReadTimeout, True), (httpx.ConnectError, False)])
def test_network_failure_certainty(failure, unknown):
    async def handle(request):
        raise failure("sensitive connection details", request=request)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ModelCallError) as caught:
                await ChatCompletionsProvider({"agent": {"apiKey": "secret"}}, client).complete(route(), [], [])
            assert caught.value.unknown is unknown
    asyncio.run(run())


@pytest.mark.parametrize("arguments", ["not-json", "[]", "null"])
def test_invalid_arguments_fail_without_fabricated_tool_result(arguments):
    def handle(request):
        call = {"id": "a", "type": "function", "function": {"name": "read_canvas", "arguments": arguments}}
        return httpx.Response(200, json=response(None, [call], "tool_calls"))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ModelCallError, match="无效") as caught:
                await ChatCompletionsProvider({"agent": {"apiKey": "secret"}}, client).complete(route(), [], [])
            assert caught.value.usage["total_tokens"] == 15
    asyncio.run(run())


def test_missing_credentials_and_context_limit_never_send():
    def forbidden(request):
        pytest.fail("Network was not expected")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            with pytest.raises(ModelCallError) as missing:
                await ChatCompletionsProvider({}, client).complete(route(), [], [])
            assert missing.value.code == "MODEL_CREDENTIAL_MISSING"
            with pytest.raises(ModelCallError) as large:
                await ChatCompletionsProvider({"agent": {"apiKey": "secret"}}, client).complete(
                    route(contextMaxBytes=4096), [{"role": "user", "content": "a" * 5000}], [])
            assert large.value.code == "CONTEXT_LIMIT"
    asyncio.run(run())


def test_route_configuration_snapshot_and_secret_separation(tmp_path):
    assert load_routes(None) == []
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"routes": [route("bailian").snapshot(), route().snapshot()]}))
    routes = load_routes(path)
    assert [r.id for r in routes] == ["deepseek", "bailian"]
    assert AgentRoute.from_snapshot(routes[0].snapshot()) == routes[0]
    assert "credentialRef" not in routes[0].public()
    invalid = route().snapshot() | {"apiKey": "must-not-be-here"}
    path.write_text(json.dumps([invalid]))
    with pytest.raises(ValueError, match="Invalid Agent route"):
        load_routes(path)


@pytest.mark.parametrize("url", ["http://model.example", "https://key@model.example", "https://model.example?key=x"])
def test_route_rejects_unsafe_deployment_endpoint(url):
    snapshot = route().snapshot() | {"baseUrl": url}
    with pytest.raises(ValueError):
        AgentRoute.from_snapshot(snapshot)
