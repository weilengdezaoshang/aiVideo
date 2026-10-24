"""Self-check model adapter. No generation tools. Production never defaults to Fake."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any, Protocol

from backend.agent.providers import ChatCompletionsProvider, ModelCallError, ModelResponse

from .contracts import SelfCheckRaw


class SelfCheckProvider(Protocol):
    kind: str

    async def complete(self, messages: list[dict], *, response_sink=None, timeout: float | None = None) -> SelfCheckRaw:
        ...


class FakeSelfCheckProvider:
    """Scripted replies for tests. Never a quality metric and never production enforce."""

    kind = "fake"

    def __init__(self, replies: list[Any] | None = None, *, sleep: float = 0, fail: str | None = None, gate=None):
        self.replies = list(replies or [{"pass": True, "problems": []}])
        self.sleep = sleep
        self.fail = fail
        self.gate = gate
        self.calls = 0

    async def complete(self, messages: list[dict], *, response_sink=None, timeout: float | None = None) -> SelfCheckRaw:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.fail == "timeout":
            raise TimeoutError("selfcheck_timeout")
        if self.fail == "unknown":
            raise ModelCallError("MODEL_RESULT_UNKNOWN", "unknown", unknown=True)
        if self.fail == "unavailable":
            raise ModelCallError("MODEL_CONNECT_ERROR", "down")
        if self.fail == "blocked":
            raise ModelCallError("CONTENT_BLOCKED", "blocked", usage={"total_tokens": 4})
        if self.fail == "blocked_model":
            raise ModelCallError("MODEL_CONTENT_BLOCKED", "blocked", usage={"total_tokens": 5})
        index = min(self.calls - 1, len(self.replies) - 1)
        reply = copy.deepcopy(self.replies[index])
        if isinstance(reply, str):
            content = reply
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            raw = {"choices": [{"message": {"content": content}}], "usage": usage}
        elif isinstance(reply, dict) and "content" in reply:
            content = str(reply["content"])
            usage = dict(reply.get("usage") or {"total_tokens": 2})
            raw = {"choices": [{"message": {"content": content}}], "usage": usage}
        else:
            content = json.dumps(reply, ensure_ascii=False)
            usage = {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
            raw = {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": usage}
        if self.fail == "sink":
            if response_sink is not None:
                await response_sink(raw)
            raise RuntimeError("sink_crash")
        if response_sink is not None:
            await response_sink(raw)
        return SelfCheckRaw(content=content, usage=usage, raw=raw)


class ChatSelfCheckProvider:
    """Reuses ChatCompletionsProvider. Empty tool list."""

    kind = "chat"

    def __init__(self, inner: ChatCompletionsProvider, route):
        self.inner = inner
        self.route = route

    async def complete(self, messages: list[dict], *, response_sink=None, timeout: float | None = None) -> SelfCheckRaw:
        response: ModelResponse = await self.inner.complete(self.route, messages, tools=[], response_sink=response_sink)
        usage = response.usage if isinstance(response.usage, dict) else {}
        raw = response.raw_response if isinstance(response.raw_response, dict) else {}
        return SelfCheckRaw(content=response.content or "", usage=usage, raw=raw)
