"""Vision-model adapter for the image_analyze tool. Structured output, no artifacts.

The network call happens inside submit (outside any worker transaction). A
durable marker file lets a crashed worker recover the finished result without
calling the model twice, mirroring the local adapter pattern.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

from backend.agent.providers import ModelCallError

from ..recognition.contracts import AnalyzerUnavailable
from .contracts import ProviderResult


class VisionAdapter:
    idempotent = True
    query_registration = True
    cancellable = False

    def __init__(self, tool, analyzer, root: Path):
        self.tool, self.analyzer, self.root = tool, analyzer, Path(root)
        self.max_image_bytes = int((tool.limits or {}).get("max_image_bytes") or 20 * 1024 * 1024)

    async def submit(self, registration_id: str, params: dict, idempotency_key: str | None):
        try:
            source = params.get("image") or {}
            path = (self.root / source["storage_key"]).resolve()
            if not path.is_relative_to(self.root.resolve()):
                return ProviderResult("failed", registration_id, failure="param_invalid")
            data = await asyncio.to_thread(path.read_bytes)
            if len(data) > self.max_image_bytes:
                return ProviderResult("failed", registration_id, failure="param_invalid")
            answer = await asyncio.wait_for(
                self.analyzer.answer(data, str(params.get("question") or ""),
                    ext=source.get("ext") or "png"), timeout=self.tool.execution.sla_seconds)
            result = ProviderResult("succeeded", registration_id,
                output=answer.model_dump(mode="json"), actual_credits=self.tool.price.credits or 0)
        except AnalyzerUnavailable:
            return ProviderResult("failed", registration_id, failure="provider_down")
        except ModelCallError as exc:
            failure = "blocked_by_model" if getattr(exc, "code", "") == "CONTENT_BLOCKED" else "provider_down"
            return ProviderResult("failed", registration_id, failure=failure)
        except (TimeoutError, OSError):
            return ProviderResult("failed", registration_id, failure="provider_down")
        except (ValueError, KeyError, TypeError):
            return ProviderResult("failed", registration_id, failure="param_invalid")
        marker = self.root / "vision-work" / registration_id / str(params["_execution_epoch"]) / "execution-ready.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        # Atomic publish: a half-written marker must never look like a durable result.
        tmp = marker.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(result)), encoding="utf-8")
        os.replace(tmp, marker)
        return result

    async def query(self, task_id, registration_id):
        for marker in (self.root / "vision-work" / registration_id).glob("*/execution-ready.json"):
            return ProviderResult(**json.loads(marker.read_text(encoding="utf-8")))
        return None

    async def download(self, locator, kind):
        raise ValueError("vision analysis produces no artifacts")

    async def cancel(self, task_id):
        raise ValueError("vision analysis cannot be cancelled remotely")
