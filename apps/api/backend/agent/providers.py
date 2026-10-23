"""Bounded, non-streaming Chat Completions adapters with no hidden retries."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Mapping
from pathlib import Path

import httpx

from .config import AgentRoute


class ModelCallError(Exception):
    def __init__(self, code: str, message: str, unknown: bool = False, *,
                 usage: dict | None = None, raw_response: dict | None = None,
                 request_id: str | None = None):
        super().__init__(message)
        self.code, self.message, self.unknown = code, message, unknown
        self.usage, self.raw_response, self.request_id = usage or {}, raw_response or {}, request_id


@dataclass(frozen=True)
class ModelResponse:
    content: str | None
    tool_calls: list[dict]
    usage: dict
    finish_reason: str
    raw_response: dict
    request_id: str | None = None

    def message(self) -> dict:
        message = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                "name": call["name"], "arguments": call["raw_arguments"]}}
                for call in self.tool_calls]
        return message


def _usage(raw: object) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("usage"), dict):
        return {}
    usage = raw["usage"]
    clean = {key: value for key, value in usage.items()
             if key in {"prompt_tokens", "completion_tokens", "total_tokens",
                        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
             and type(value) is int and value >= 0}
    for name in ("prompt_tokens_details", "completion_tokens_details"):
        details = usage.get(name)
        if isinstance(details, dict):
            clean[name] = {key: value for key, value in details.items()
                           if key in {"cached_tokens", "reasoning_tokens", "audio_tokens",
                                      "accepted_prediction_tokens", "rejected_prediction_tokens"}
                           and type(value) is int and value >= 0}
    return clean


def _invalid_constant(value: str):
    raise ValueError("Non-finite JSON value")


def _parse_response(raw: object, request_id: str | None) -> ModelResponse:
    clean_usage = _usage(raw)
    request_id = request_id[:256] if request_id else None
    try:
        if not isinstance(raw, dict) or not isinstance(raw.get("choices"), list):
            raise ValueError
        if len(raw["choices"]) != 1:
            raise ValueError
        choice = raw["choices"][0]
        message, finish = choice["message"], choice["finish_reason"]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError
        if not isinstance(finish, str):
            raise ValueError
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError
        calls, identifiers = [], set()
        wire_calls = message.get("tool_calls") or []
        if not isinstance(wire_calls, list) or len(wire_calls) > 128:
            raise ValueError
        for call in wire_calls:
            ident, function = call["id"], call["function"]
            if (call.get("type") != "function" or not isinstance(ident, str)
                    or not ident or len(ident) > 255 or ident in identifiers):
                raise ValueError
            name, arguments = function["name"], function["arguments"]
            if not isinstance(name, str) or not 1 <= len(name) <= 100 or not isinstance(arguments, str):
                raise ValueError
            parsed = json.loads(arguments, parse_constant=_invalid_constant)
            if not isinstance(parsed, dict):
                raise ValueError
            identifiers.add(ident)
            calls.append({"id": ident, "name": name, "arguments": parsed,
                          "raw_arguments": arguments})
        if (calls and finish != "tool_calls") or (not calls and finish == "tool_calls"):
            raise ValueError
        if not calls and not content and finish == "stop":
            raise ValueError
        usage = raw.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError
        # Only keep protocol data we use: provider reasoning/debug/headers are not history.
        clean = {key: raw[key] for key in ("id", "model", "created", "object")
                 if key in raw and isinstance(raw[key], (str, int))}
        response = ModelResponse(content, calls, clean_usage, finish, clean,
                                 request_id or (raw["id"][:256] if isinstance(raw.get("id"), str) else None))
        clean.update({"choices": [{"index": 0, "message": response.message(),
                                  "finish_reason": finish}], "usage": clean_usage})
        return response
    except (ValueError, KeyError, TypeError, IndexError, RecursionError) as error:
        raise ModelCallError("INVALID_MODEL_RESPONSE", "模型返回了无效的工具调用或响应",
                             usage=clean_usage, raw_response={"usage": clean_usage},
                             request_id=request_id) from error


class ChatCompletionsProvider:
    def __init__(self, credentials: Mapping[str, object], client: httpx.AsyncClient | None = None,
                 local_config_root: Path | None = None):
        self.credentials, self.client = credentials, client
        self.local_config_root = local_config_root

    async def complete(self, route: AgentRoute, messages: list[dict], tools: list[dict], *,
                       response_sink=None) -> ModelResponse:
        secret = self.credentials.get(route.credential_ref)
        if self.local_config_root is not None and route.credential_ref.startswith("local-dialogue:"):
            import uuid
            from .local_config import load_config
            config = load_config(self.local_config_root, uuid.UUID(route.credential_ref.split(":", 1)[1]))
            secret = {"apiKey": config.apiKey.get_secret_value()} if config and config.apiKey else None
        api_key = secret.get("apiKey") if isinstance(secret, dict) else None
        if not isinstance(api_key, str) or not api_key.strip():
            raise ModelCallError("MODEL_CREDENTIAL_MISSING", "模型凭据尚未配置")
        payload = {"model": route.model, "messages": messages, "tools": tools,
                   "tool_choice": "auto", "stream": False, "max_tokens": route.max_tokens}
        # Official non-thinking switches differ even though both use Chat Completions.
        if route.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        elif route.id != "local-dialogue" or route.model.lower().startswith("qwen"):
            payload["enable_thinking"] = False
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > route.context_max_bytes:
            raise ModelCallError("CONTEXT_LIMIT", "模型请求超过上下文大小限制")
        if self.client is not None:
            return await self._request(self.client, route, encoded, api_key, response_sink)
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            return await self._request(client, route, encoded, api_key, response_sink)

    async def _request(self, client: httpx.AsyncClient, route: AgentRoute,
                       encoded: bytes, api_key: str, response_sink=None) -> ModelResponse:
        try:
            async with asyncio.timeout(route.timeout_s):
                async with client.stream("POST", route.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                    content=encoded, timeout=route.timeout_s, follow_redirects=False) as response:
                    if not 200 <= response.status_code < 300:
                        raise ModelCallError("MODEL_HTTP_ERROR", f"模型服务返回 HTTP {response.status_code}",
                                             unknown=response.status_code >= 500)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            raise ModelCallError("MODEL_RESPONSE_LIMIT", "模型响应超过大小限制")
                    try:
                        raw = json.loads(body, parse_constant=_invalid_constant)
                    except (ValueError, UnicodeError, RecursionError) as error:
                        if response_sink is not None:
                            await response_sink(_redacted_failure(body))
                        raise ModelCallError("INVALID_MODEL_RESPONSE", "模型响应不是有效 JSON") from error
                    if response_sink is not None:
                        await response_sink(_redacted_raw(raw))
                    return _parse_response(raw, response.headers.get("x-request-id"))
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            raise ModelCallError("MODEL_CONNECT_ERROR", "无法连接模型服务") from error
        except (httpx.TransportError, TimeoutError) as error:
            # Local timeout/disconnect cannot establish that no paid request happened.
            raise ModelCallError("MODEL_RESULT_UNKNOWN", "模型请求结果未知，未自动重试", unknown=True) from error


_SECRET = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}")


def _redacted_raw(raw: dict) -> dict:
    """Keep a complete choices document. Do not slice JSON text before parsing it."""

    if not isinstance(raw, dict):
        return {"invalid": True}
    choices = raw.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content") if isinstance(message.get("content"), str) else None
    calls = []
    wire = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
    for call in wire:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        arguments = function.get("arguments") if isinstance(function.get("arguments"), str) else ""
        calls.append({
            "id": call.get("id") if isinstance(call.get("id"), str) else "",
            "type": "function",
            "function": {
                "name": function.get("name") if isinstance(function.get("name"), str) else "",
                "arguments": arguments,
            },
        })
    finish = choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else ""
    clean = {
        "id": raw["id"] if isinstance(raw.get("id"), str) else "",
        "model": raw["model"] if isinstance(raw.get("model"), str) else "",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content, "tool_calls": calls},
                     "finish_reason": finish}],
        "usage": _usage(raw),
    }
    return json.loads(_SECRET.sub(r"\1[redacted]", json.dumps(clean, ensure_ascii=False)))


def _redacted_failure(body: bytearray) -> dict:
    text = bytes(body[:500]).decode("utf-8", errors="replace")
    return {"invalid_json": True, "bytes": len(body), "excerpt": _SECRET.sub(r"\1[redacted]", text)}
