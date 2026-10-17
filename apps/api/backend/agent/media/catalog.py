"""Deployment-owned, immutable capability bindings. No browser or prompt can choose models."""
from __future__ import annotations

import json
from pathlib import Path

from .contracts import Capability, CatalogConfig, MediaPlan


class HostError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Catalog:
    def __init__(self, config: CatalogConfig):
        self.config = config
        if config.enabled and (config.reviewMode != "off" or any(cap.enabled and cap.moderation == "required" for cap in config.capabilities)) and config.reviewer is None:
            raise ValueError("Reviewer or required moderation needs an explicit vision route")
        self.entries = {cap.id: cap for cap in config.capabilities}
        if len(self.entries) != len(config.capabilities):
            raise ValueError("Duplicate capability ID")
        for cap in config.capabilities:
            if (config.environment == "production" and cap.enabled and cap.binding
                    and cap.binding.providerSnapshot.get("provider") == "mock"):
                raise ValueError("prod_mock: production Host cannot enable Mock bindings")

    @classmethod
    def load(cls, path: Path | None):
        if path is None:
            return cls(CatalogConfig(version="disabled-v1"))
        return cls(CatalogConfig.model_validate(json.loads(path.read_text())))

    def resolve(self, capability_id: str, tier: str) -> Capability:
        cap = self.entries.get(capability_id)
        if not self.config.enabled or cap is None or not cap.enabled:
            raise HostError("capability_unavailable", "该创作能力暂不可用", status=503)
        if cap.binding is None:
            raise HostError("capability_unbound", "该创作能力尚未绑定", status=503)
        if tier not in cap.tiers:
            raise HostError("spec_not_allowed", "当前能力不支持此生成阶段", status=400)
        return cap

    def public(self):
        return {"enabled": self.config.enabled, "version": self.config.version,
            "capabilities": [{"id": cap.id, "enabled": self.config.enabled and cap.enabled and bool(cap.binding),
                "outputType": cap.outputType, "maxRefImages": cap.maxRefImages,
                "tiers": {tier: {"width": spec.width, "height": spec.height,
                    "durationSec": spec.durationSec, "fps": spec.fps} for tier, spec in cap.tiers.items()}}
                for cap in self.entries.values()]}

    def quote(self, plan: MediaPlan) -> int:
        return sum(self.resolve(step.capabilityId, step.tier).tiers[step.tier].credits * step.fanOut
                   for step in plan.steps)
