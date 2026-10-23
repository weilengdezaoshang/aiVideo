"""Planner binding for evaluation runs.

Rule mode constructs RulePlanner. LLM mode uses an injected factory when one is
set. Otherwise it builds LLMPlanner from ``wiring.deployment_routes`` and
``ChatCompletionsProvider``. No route or no credential is unavailable. This
module does not copy model names, endpoints, or keys, and it does not own a
second prompt.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
from typing import Any, Callable

from backend.core.supervisor.planners import RulePlanner
from backend.evaluation.planner_evaluation import mock_catalog

PlannerFactory = Callable[[], Any]

ASSEMBLER_MODULE = "backend.core.supervisor.context"
PROMPT_MODULE = "backend.core.supervisor.prompts"

_FACTORY: PlannerFactory | None = None

LLM_FACTORY_GAP = (
    "没有注入 factory，deployment_routes 也没有部署 agent route。"
    "记 undetermined，不回退 Rule，不记通过。"
)

UNATTESTED_REASON = (
    "注入对象缺少可审计 eval_binding（kind=synthetic、synthetic=true、audit.source）。"
    "没有 synthetic 属性不能当成真实 LLM。"
)

UNWIRED_LLM_REASON = (
    "注入对象声明 kind=llm 不能代替部署默认构造。"
    "真实绑定只来自 wiring.deployment_routes 与 ChatCompletionsProvider；"
    "llmTested 保持 false。"
)

MISSING_CREDENTIAL_REASON = (
    "部署 route 已选定，但凭据映射里没有可用的 apiKey。"
    "记 unavailable，不哈希密钥，不回退 Rule。"
)

UNKNOWN_ROUTE_REASON = "plannerRouteId 不在 deployment_routes 中。不接受客户端 URL 或密钥。"


def set_planner_factory(factory: PlannerFactory | None) -> None:
    """Replace the process-wide LLM planner constructor. `None` clears it."""

    global _FACTORY
    _FACTORY = factory


def get_planner_factory() -> PlannerFactory | None:
    return _FACTORY


def synthetic_eval_binding(source: str) -> dict[str, Any]:
    """Audit metadata for an injected protocol fake. This is not a model route."""

    if not isinstance(source, str) or not source.strip():
        raise ValueError("synthetic eval_binding 需要非空 audit.source")
    return {"kind": "synthetic", "synthetic": True, "audit": {"source": source.strip()}}


def resolve_assemble_messages() -> tuple[Callable[..., Any], str]:
    """Return the core assembler. A missing core module is an error, not a short fallback."""

    try:
        module = importlib.import_module(ASSEMBLER_MODULE)
    except ImportError as exc:
        raise RuntimeError(
            "评测提示词依赖 backend.core.supervisor.context，模块不存在。"
            "拒绝搜索 llm/llm_planner，也拒绝复制短 system fallback。"
        ) from exc
    candidate = getattr(module, "assemble_messages", None)
    if not callable(candidate):
        raise RuntimeError(
            "backend.core.supervisor.context 没有 assemble_messages。"
            "评测不会改用另一份模板。"
        )
    return candidate, ASSEMBLER_MODULE


def prompt_template_evidence() -> dict[str, Any]:
    """Hash the loaded supervisor template text. Assembler source is a separate digest."""

    function, module_name = resolve_assemble_messages()
    try:
        prompts = importlib.import_module(PROMPT_MODULE)
        load_prompt = prompts.load_prompt
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "评测提示词依赖 backend.core.supervisor.prompts.load_prompt，无法加载。"
            "拒绝用 assembler 源码哈希冒充模板哈希。"
        ) from exc
    prompt = load_prompt()
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError) as exc:
        raise RuntimeError(
            "无法读取 assemble_messages 源码。模板哈希已从 load_prompt 取得，"
            "但缺少独立的 assembler 源码哈希，拒绝不完整证据。"
        ) from exc
    if not getattr(prompt, "version", None) or not getattr(prompt, "content_hash", None):
        raise RuntimeError("load_prompt 没有返回 version/content_hash。")
    return {
        "promptTemplateVersion": prompt.version,
        "promptTemplateHash": prompt.content_hash,
        "promptTemplateModule": PROMPT_MODULE,
        "promptAssemblerModule": module_name,
        "promptAssemblerSourceHash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "llmPromptImplemented": True,
    }


def tool_catalog_evidence() -> dict[str, str]:
    catalog = mock_catalog()
    return {
        "toolCatalogVersion": catalog.snapshot.version,
        "toolCatalogHash": catalog.lock.content_hash,
    }


def _synthetic_binding(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("kind") != "synthetic" or raw.get("synthetic") is not True:
        return None
    audit = raw.get("audit")
    if not isinstance(audit, dict):
        return None
    source = audit.get("source")
    if not isinstance(source, str) or not source.strip():
        return None
    return {"kind": "synthetic", "synthetic": True, "audit": {"source": source.strip()}}


def _credential_ready(credentials: dict, route: Any) -> bool:
    """True when the route's credential map entry has an apiKey. The value is not copied."""

    secret = credentials.get(route.credential_ref)
    if not isinstance(secret, dict):
        return False
    api_key = secret.get("apiKey")
    return isinstance(api_key, str) and bool(api_key.strip())


def _deployment_binding(route_id: str | None) -> dict[str, Any]:
    """LLMPlanner from the deployment route list. Missing config stays undetermined."""

    from backend.agent.providers import ChatCompletionsProvider
    from backend.core.supervisor.planners import LLMPlanner
    from backend.core.supervisor.wiring import deployment_routes, development_root, load_credential_map, preferred_route

    routes = deployment_routes()
    if route_id:
        route = next((item for item in routes if item.id == route_id), None)
        if route is None:
            return _unavailable("llm_unavailable", UNKNOWN_ROUTE_REASON)
    else:
        route = preferred_route(routes, None)
        if route is None:
            return _unavailable("llm_unavailable", LLM_FACTORY_GAP)
    credentials = load_credential_map()
    if not _credential_ready(credentials, route):
        return _unavailable("llm_unavailable", MISSING_CREDENTIAL_REASON)
    # development_root is None outside an explicit development runtime, so tests do not open local key files.
    provider = ChatCompletionsProvider(credentials, local_config_root=development_root())
    planner = LLMPlanner(provider, route, factory=None, settings=None)
    audit = {"source": "backend.core.supervisor.wiring.deployment_routes", "routeId": route.id}
    planner.eval_binding = {"kind": "llm", "synthetic": False, "audit": audit}
    return {
        "planner": planner,
        "mode": "llm",
        "llm_tested": True,
        "synthetic": False,
        "unavailable": False,
        "reason": "部署默认构造的 LLMPlanner。质量分来自该 route 的真实调用。",
        "eval_binding": {"kind": "llm", "synthetic": False, "audit": dict(audit)},
    }


def _unavailable(mode: str, reason: str) -> dict[str, Any]:
    return {
        "planner": None,
        "mode": mode,
        "llm_tested": False,
        "synthetic": False,
        "unavailable": True,
        "reason": reason,
        "eval_binding": None,
    }


def bind_planner(planner_mode: str, route_id: str | None = None) -> dict[str, Any]:
    """Return the planner object for one run, or an unavailable binding.

    No factory and no deployment route → undetermined. An injected object is a
    protocol fake only with ``eval_binding``. Declared ``kind=llm`` on an
    injected object is not the deployment constructor.
    """

    if planner_mode == "rule":
        return {
            "planner": RulePlanner(),
            "mode": "rule_baseline",
            "llm_tested": False,
            "synthetic": False,
            "unavailable": False,
            "reason": "RulePlanner 基线，未调用 LLM。",
            "eval_binding": None,
        }
    if planner_mode != "llm":
        raise ValueError("plannerMode 只能是 rule 或 llm")
    factory = _FACTORY
    if factory is None:
        return _deployment_binding(route_id)
    planner = factory()
    claimed = getattr(planner, "eval_binding", None)
    if isinstance(claimed, dict) and claimed.get("kind") == "llm":
        return {
            "planner": None,
            "mode": "llm_unconfigured",
            "llm_tested": False,
            "synthetic": False,
            "unavailable": True,
            "reason": UNWIRED_LLM_REASON,
            "eval_binding": {
                "kind": "llm",
                "synthetic": False,
                "accepted": False,
                "audit_present": isinstance(claimed.get("audit"), dict),
            },
        }
    binding = _synthetic_binding(claimed)
    if binding is None:
        return {
            "planner": None,
            "mode": "llm_unattested",
            "llm_tested": False,
            "synthetic": False,
            "unavailable": True,
            "reason": UNATTESTED_REASON,
            "eval_binding": None,
        }
    return {
        "planner": planner,
        "mode": "synthetic",
        "llm_tested": False,
        "synthetic": True,
        "unavailable": False,
        "reason": "注入的 fake planner 带 synthetic eval_binding，只验证 plan(context) 协议，不代表模型质量。",
        "eval_binding": binding,
    }
