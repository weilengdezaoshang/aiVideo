"""Detached immutable snapshots: all exports use the same version and content hash."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from collections.abc import Mapping

from jsonschema import Draft202012Validator

from .contracts import CatalogSnapshot, ToolDefinition, VersionLock, ToolSettings


def canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class CatalogError(ValueError):
    pass


@lru_cache(maxsize=ToolSettings().catalog_validation_cache_size)
def _validated_snapshot(encoded: str) -> CatalogSnapshot:
    # Cache by full canonical content, never version alone. No mutable value is
    # exported; public accessors return deep copies. Database integrity is still
    # checked on every load, including every definition record.
    return CatalogSnapshot.model_validate_json(encoded)


class ToolCatalog:
    def __init__(self, snapshot: CatalogSnapshot):
        self._initialize(canonical(snapshot.model_dump(mode="json")))

    @classmethod
    def from_document(cls, document: dict) -> ToolCatalog:
        catalog = cls.__new__(cls)
        catalog._initialize(canonical(document))
        return catalog

    def _initialize(self, encoded: str):
        self._encoded = encoded
        self._hash = hashlib.sha256(self._encoded.encode()).hexdigest()
        self._snapshot = _validated_snapshot(self._encoded)
        self._version = self._snapshot.version
        self._tools = {tool.name: tool for tool in self._snapshot.tools}

    @property
    def snapshot(self) -> CatalogSnapshot:
        # Pydantic frozen is shallow; callers always receive a detached copy.
        return self._snapshot.model_copy(deep=True)

    @property
    def lock(self) -> VersionLock:
        return VersionLock(version=self._version, content_hash=self._hash)

    def check_lock(self, version_lock: VersionLock):
        if version_lock != self.lock:
            raise CatalogError("catalog_version_lock_mismatch")

    def get_tool(self, name: str, version: str) -> ToolDefinition:
        if version != self.lock.version:
            raise CatalogError("catalog_version_not_loaded")
        tool = self._tools.get(name)
        if tool is None:
            raise CatalogError("tool_not_found")
        return tool.model_copy(deep=True)

    def to_model_tools(self, user: Mapping[str, object], version_lock: VersionLock) -> list[dict]:
        self.check_lock(version_lock)
        group = user.get("rollout_group")
        return [{"type": "function", "function": {
            "name": tool.name,
            "description": tool.description + " 此调用仅描述任务单，须审核后由工人执行。",
            "parameters": tool.parameters}}
            for tool in self.snapshot.tools if tool.available
            and (not tool.rollout_groups or group in tool.rollout_groups)]

    def to_validator_rules(self, version: str) -> dict:
        if version != self.lock.version:
            raise CatalogError("catalog_version_not_loaded")
        return {"version_lock": self.lock.model_dump(), "tools": {
            t.name: {key: t.model_dump(mode="json")[key] for key in (
                "parameters", "result_schema", "inputs", "outputs", "limits", "price", "available", "rollout_groups")}
            for t in self.snapshot.tools}}

    def to_worker_config(self, name: str, version_lock: VersionLock) -> dict:
        self.check_lock(version_lock)
        tool = self.get_tool(name, version_lock.version)
        if not tool.available:
            raise CatalogError("tool_unavailable")
        return {"name": tool.name, "version": tool.version, "version_lock": self.lock.model_dump(),
                "mode": tool.mode, "execution": tool.execution.model_dump(mode="json"),
                "failures": [r.model_dump() for r in tool.failures], "price": tool.price.model_dump()}


def estimate_cost(tool: ToolDefinition, params: dict) -> int:
    Draft202012Validator(tool.parameters).validate(params)
    if not tool.available or tool.price.kind == "unpriced":
        raise CatalogError("tool_unavailable_or_unpriced")
    if tool.price.kind == "fixed":
        return tool.price.credits
    count = params.get(tool.price.count_parameter)
    if type(count) is not int or count < 1:
        raise CatalogError("explicit_positive_count_required")
    return tool.price.credits * count
