"""Select Rule or Cascade from the locked Run. Credentials stay outside the Run."""
import os
from pathlib import Path

from backend.agent.config import AgentRoute, load_credentials, load_routes
from backend.agent.providers import ChatCompletionsProvider

from .contracts import Settings
from .planners import CascadePlanner, LLMPlanner, RulePlanner


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


def build_supervisor(factory, settings: Settings | None = None):
    from .engine import Supervisor
    return Supervisor(factory, settings=settings or Settings(), credentials=load_credential_map(),
        local_root=development_root(), routes=deployment_routes())
