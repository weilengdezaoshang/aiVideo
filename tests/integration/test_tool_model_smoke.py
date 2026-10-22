"""Explicit real Chat Completions schema smoke. Never calls generation tools.

AIVERO_TOOL_SMOKE_ROUTES / AIVERO_TOOL_SMOKE_CREDENTIALS must name explicitly
approved local files. Optional AIVERO_TOOL_SMOKE_ROUTE selects a route ID.
No discovery of credentials in user data, production databases, or root config.
"""
import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from backend.agent.config import load_routes
from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.agent.local_config import dialogue_route
from backend.agent.providers import ChatCompletionsProvider
from backend.core.tools import build_catalog

ROUTES = os.environ.get("AIVERO_TOOL_SMOKE_ROUTES")
CREDENTIALS = os.environ.get("AIVERO_TOOL_SMOKE_CREDENTIALS")
LOCAL_CONFIG = os.environ.get("AIVERO_TOOL_SMOKE_LOCAL_CONFIG")
pytestmark = pytest.mark.skipif(not (LOCAL_CONFIG or (ROUTES and CREDENTIALS)), reason="explicit real model configuration required")


def test_real_model_accepts_exported_tool_schemas():
    async def scenario():
        local_root = None
        if LOCAL_CONFIG:
            path = Path(LOCAL_CONFIG).resolve()
            assert path.parent.name == "private-agent-config"
            local_root = path.parent.parent
            route = dialogue_route(local_root, uuid.UUID(path.stem))
            assert route is not None, "local dialogue key not configured"
            credentials = {}
        else:
            routes = load_routes(Path(ROUTES))
            selected = os.environ.get("AIVERO_TOOL_SMOKE_ROUTE")
            route = next((r for r in routes if r.id == selected), None) if selected else routes[0]
            assert route is not None, "configured smoke route not found"
            credentials = json.loads(Path(CREDENTIALS).read_text())
        host_path = os.environ.get("AIVERO_TOOL_SMOKE_HOST_CATALOG")
        if host_path:
            host = Catalog.load(Path(host_path))
        else:
            # Definitions-only fixture. Mock bindings are never executed; the real
            # dialogue model sees every implemented family of schema, not just reads.
            raw = json.loads(Path("deploy/host-catalog.development.json").read_text())
            raw["enabled"] = True
            existing = {cap["id"] for cap in raw["capabilities"]}
            for name in ("text_to_video", "image_to_video"):
                if name not in existing:
                    raw["capabilities"].append({"id": name, "outputType": "video",
                        "maxRefImages": 1 if name == "image_to_video" else 0,
                        "tiers": {"preview": {"credits": 1}},
                        "binding": {"version": "schema-only", "providerSnapshot": {
                            "provider": "mock", "configVersion": "schema-only", "videoModel": "mock"}}})
            for cap in raw["capabilities"]:
                cap["enabled"] = True
            host = Catalog(CatalogConfig.model_validate(raw))
        catalog = build_catalog(host, "real-smoke")
        schemas = catalog.to_model_tools({}, catalog.lock)
        result = await ChatCompletionsProvider(credentials, local_config_root=local_root).complete(route, [
            {"role": "system", "content": "This is a schema acceptance test. Describe one read_canvas task using its function schema. No function is executed."},
            {"role": "user", "content": "Please read the canvas overview with read_canvas."}], schemas)
        assert result.tool_calls, "model accepted schemas but did not return the requested read-only proposal"
        for call in result.tool_calls:
            definition = catalog.get_tool(call["name"], catalog.lock.version)
            assert definition.mode == "read_only"
            Draft202012Validator(definition.parameters).validate(call["arguments"])
    asyncio.run(scenario())
