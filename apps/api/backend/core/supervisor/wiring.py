"""Select Rule or Cascade from the locked Run. Credentials stay outside the Run."""
import json
import logging
import os
from pathlib import Path

from backend.agent.config import AgentRoute, load_credentials, load_routes
from backend.agent.providers import ChatCompletionsProvider
from backend.core.selfcheck.contracts import EnforcementEvidence, POLICY_HASH
from backend.core.selfcheck.policy import policy_fingerprint
from backend.core.selfcheck.prompt import load_prompt
from backend.core.selfcheck.provider import ChatSelfCheckProvider

from .contracts import Settings
from .planners import CascadePlanner, LLMPlanner, RulePlanner

_AUDIT = logging.getLogger("backend.core.supervisor.audit")


def load_credential_map() -> dict:
    return load_credentials(os.environ.get("AIVERO_CREDENTIALS_FILE"))


def deployment_routes() -> list[AgentRoute]:
    path = os.environ.get("AIVERO_PRODUCTION_AGENT_ROUTES_FILE")
    if not path:
        return []
    return load_routes(path)


def development_root() -> Path | None:
    if os.environ.get("AIVERO_RUNTIME_ENVIRONMENT") != "development":
        return None
    root = os.environ.get("AIVERO_PRODUCTION_MEDIA_ROOT") or os.environ.get("SWARMUI_DATA_DIR")
    return Path(root) if root else None


def preferred_route(routes: list[AgentRoute], local: AgentRoute | None) -> AgentRoute | None:
    if local is not None:
        return local
    return next((route for route in routes if route.provider == "deepseek"), routes[0] if routes else None)


def route_snapshot(route: AgentRoute | None) -> dict | None:
    if route is None:
        return None
    snapshot = route.snapshot()
    snapshot.pop("apiKey", None)
    return snapshot


def planner_for_lock(run, *, factory, settings: Settings, credentials: dict, local_root: Path | None, explicit=None):
    if explicit is not None:
        return explicit
    lock = run.version_lock or {}
    if lock.get("supervisor") != "llm-v1":
        return RulePlanner()
    raw = lock.get("agent_route")
    llm = None
    if isinstance(raw, dict) and raw:
        route = AgentRoute.from_snapshot(raw)
        provider = ChatCompletionsProvider(credentials, local_config_root=local_root)
        llm = LLMPlanner(provider, route, factory=factory, settings=settings)
    return CascadePlanner(llm, settings)


def watchdog_seconds(run, settings: Settings) -> float:
    """Outer wait is the locked route timeout plus the checkpoint margin."""

    raw = (run.version_lock or {}).get("agent_route") or {}
    timeout = raw.get("timeoutSeconds", raw.get("timeout_s")) if isinstance(raw, dict) else None
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        return float(timeout) + settings.planner_checkpoint_margin_seconds
    return settings.planner_timeout_seconds


def frozen_selfcheck_route(run) -> dict | None:
    """Frozen selfcheck route on the Run. Older runs without a selfcheck entry use agent_route."""

    lock = getattr(run, "version_lock", None) or {}
    entry = lock.get("selfcheck")
    if isinstance(entry, dict):
        raw = entry.get("route")
        return raw if isinstance(raw, dict) and raw else None
    raw = lock.get("agent_route")
    return raw if isinstance(raw, dict) and raw else None


def freeze_selfcheck_entry(settings: Settings, agent_route_snapshot, *, routes=None) -> dict:
    """Snapshot chosen at Run create. Registry changes after create do not affect this Run."""

    prompt = load_prompt(settings.selfcheck_template_version)
    requested = (settings.selfcheck_route_id or "").strip() or None
    chosen = None
    reason = None
    if requested:
        for route in routes or []:
            if getattr(route, "id", None) == requested:
                chosen = route_snapshot(route)
                break
        if chosen is None:
            reason = "missing_route_id"
    elif isinstance(agent_route_snapshot, dict) and agent_route_snapshot:
        chosen = dict(agent_route_snapshot)
    else:
        reason = "no_route"
    if isinstance(chosen, dict):
        chosen = dict(chosen)
        chosen.pop("apiKey", None)
        chosen.pop("api_key", None)
    return {
        "route": chosen,
        "route_id": requested or (chosen or {}).get("id"),
        "reason": reason,
        "template_version": prompt.version,
        "template_hash": prompt.content_hash,
        "policy_hash": POLICY_HASH,
        "policy_fingerprint": policy_fingerprint(settings),
    }


def load_selfcheck_evidence(settings: Settings):
    """Trusted report JSON only. Never treats this path as a credential file. Fail closed."""

    path = (settings.selfcheck_evidence_path or "").strip()
    if not path:
        return None, {"reason": "missing_evidence_path"}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        _AUDIT.info("selfcheck evidence unreadable path=%s reason=%s", path, type(exc).__name__)
        return None, {"reason": "evidence_unreadable", "detail": type(exc).__name__}
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("evidence_not_object")
        evidence = EnforcementEvidence.model_validate(data)
    except Exception as exc:
        _AUDIT.info("selfcheck evidence invalid path=%s reason=%s", path, type(exc).__name__)
        return None, {"reason": "evidence_invalid", "detail": type(exc).__name__}
    return evidence, {"reason": "loaded", "dataset_hash": evidence.dataset_hash}


def selfcheck_provider_for(run, *, credentials: dict, local_root: Path | None, routes: list | None, explicit=None):
    """Frozen configured route only. Missing route returns None (unavailable), never Fake."""

    if explicit is not None:
        return explicit
    raw = frozen_selfcheck_route(run)
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        route = AgentRoute.from_snapshot(raw)
    except (ValueError, TypeError):
        return None
    return ChatSelfCheckProvider(ChatCompletionsProvider(credentials, local_config_root=local_root), route)


def build_supervisor(factory, settings: Settings | None = None):
    from .engine import Supervisor
    settings = settings or Settings()
    evidence, trace = load_selfcheck_evidence(settings)
    if trace.get("reason") != "loaded":
        _AUDIT.info("selfcheck evidence not loaded reason=%s", trace.get("reason"))
    return Supervisor(factory, settings=settings, credentials=load_credential_map(),
        local_root=development_root(), routes=deployment_routes(), selfcheck_evidence=evidence)
