"""Self-check model binding. No implicit provider and no key arguments."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from uuid import UUID

SelfCheckFactory = Callable[[], Any]

_FACTORY: SelfCheckFactory | None = None

MISSING_BINDING = "没有显式 factory，也没有 route id 或本机对话配置。记 unknown，不回退规则通过。"
UNKNOWN_ROUTE = "route id 不在 deployment_routes 中。不接受客户端 URL 或密钥。"
MISSING_CREDENTIAL = "部署 route 已选定，但凭据映射没有可用 apiKey。不哈希密钥。"
MISSING_LOCAL = "本机对话配置不存在或没有密钥。评测不接收密钥参数。"


def set_selfcheck_factory(factory: SelfCheckFactory | None) -> None:
    global _FACTORY
    _FACTORY = factory


def get_selfcheck_factory() -> SelfCheckFactory | None:
    return _FACTORY


def synthetic_eval_binding(source: str) -> dict[str, Any]:
    if not isinstance(source, str) or not source.strip():
        raise ValueError("synthetic eval_binding 需要非空 audit.source")
    return {"kind": "synthetic", "synthetic": True, "audit": {"source": source.strip()}}


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "provider": None,
        "mode": "llm",
        "llm_tested": False,
        "unavailable": reason,
        "route": {},
        "model": None,
    }


def _live(provider, route, source: str) -> dict[str, Any]:
    from backend.core.selfcheck.policy import public_route

    provider.eval_binding = {
        "kind": "llm",
        "synthetic": False,
        "audit": {"source": source, "routeId": route.id},
    }
    snapshot = public_route(route.snapshot())
    return {
        "provider": provider,
        "mode": "llm",
        "llm_tested": False,
        "unavailable": None,
        "route": snapshot,
        "model": route.model,
        "route_id": route.id,
    }


def bind_selfcheck(
    mode: str,
    route_id: str | None = None,
    *,
    local_config_root: Path | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    if mode in {"rules", "offline"}:
        return {
            "provider": None,
            "mode": mode,
            "llm_tested": False,
            "unavailable": None,
            "route": {},
            "model": None,
        }
    if mode != "llm":
        raise ValueError("selfcheck mode 只能是 rules、offline 或 llm")
    if route_id and (local_config_root or workspace_id):
        raise ValueError("route id 与本机对话配置只能选一种")
    factory = _FACTORY
    if factory is not None:
        provider = factory()
        binding = getattr(provider, "eval_binding", None)
        if not isinstance(binding, dict) or binding.get("synthetic") is not True:
            return _unavailable(
                "注入 factory 不是带 synthetic 标记的协议替身，也不能代替部署 route。"
            )
        return {
            "provider": provider,
            "mode": "protocol",
            "llm_tested": False,
            "unavailable": None,
            "route": {},
            "model": None,
            "synthetic": True,
        }
    if local_config_root is not None or workspace_id is not None:
        if local_config_root is None or not workspace_id:
            return _unavailable(MISSING_LOCAL)
        try:
            workspace = UUID(workspace_id)
        except ValueError:
            return _unavailable(MISSING_LOCAL)
        from backend.agent.local_config import dialogue_route
        from backend.agent.providers import ChatCompletionsProvider
        from backend.core.selfcheck.provider import ChatSelfCheckProvider

        route = dialogue_route(Path(local_config_root), workspace)
        if route is None:
            return _unavailable(MISSING_LOCAL)
        inner = ChatCompletionsProvider({}, local_config_root=Path(local_config_root))
        return _live(ChatSelfCheckProvider(inner, route), route, "backend.agent.local_config.dialogue_route")
    if not route_id:
        return _unavailable(MISSING_BINDING)
    from backend.agent.providers import ChatCompletionsProvider
    from backend.core.selfcheck.provider import ChatSelfCheckProvider
    from backend.core.supervisor.wiring import deployment_routes, load_credential_map

    routes = deployment_routes()
    route = next((item for item in routes if item.id == route_id), None)
    if route is None:
        return _unavailable(UNKNOWN_ROUTE)
    secret = load_credential_map().get(route.credential_ref)
    api_key = secret.get("apiKey") if isinstance(secret, dict) else None
    if not isinstance(api_key, str) or not api_key.strip():
        return _unavailable(MISSING_CREDENTIAL)
    inner = ChatCompletionsProvider(load_credential_map())
    return _live(ChatSelfCheckProvider(inner, route), route, "backend.core.supervisor.wiring.deployment_routes")
