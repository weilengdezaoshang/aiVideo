"""One network phase per invocation. Callers persist the returned handle before polling."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import PurePosixPath

from backend.config import Config
from backend.models import GenParams, InitImage
from backend.providers.base import Generated
from backend.providers.cloud import CloudProvider
from backend.providers.comfyui import ComfyProvider
from backend.providers.mock import MockProvider
from backend.providers.workflows import image_workflow, ltx_workflow, wan_workflow


@dataclass(frozen=True)
class Capabilities:
    asynchronous: bool
    idempotent_submit: bool = False
    query_by_request: bool = False
    cancel: bool = False


@dataclass(frozen=True)
class StageResult:
    phase: str
    payload: dict
    external_id: str | None = None
    generated: Generated | None = None
    finished: bool = False


class StagedProvider:
    def __init__(self, config: Config, client):
        self.config, self.client = config, client
        self.cloud = CloudProvider(config, client)
        self.comfy = ComfyProvider(config, client)

    def capabilities(self, kind: str) -> Capabilities:
        return Capabilities(
            asynchronous=self.config.provider == "comfyui" or (
                self.config.provider == "cloud" and kind == "video"),
            cancel=self.config.provider == "cloud" and kind == "video",
        )

    async def prepare(self, params: GenParams, seed: int,
                      image: InitImage | None = None, mask: InitImage | None = None) -> StageResult:
        if self.config.provider == "mock":
            return StageResult("submit", {})
        if self.config.provider == "cloud":
            if params.kind == "video":
                if image or mask:
                    raise ValueError("云端图生视频暂未开放")
                self.cloud.video_request(params)
            else:
                self.cloud.request(params, seed, image, mask)
            return StageResult("submit", {})
        ref = await self.comfy.upload(image) if image else None
        mask_name = await self.comfy.upload(mask) if image and mask else None
        if params.kind == "video":
            backend, model, low = await self.comfy.video_model()
            workflow = (ltx_workflow(params, seed, model, ref) if backend == "ltxv"
                        else wan_workflow(params, seed, model, self.config.wanClip,
                                          self.config.wanVae, ref, low))
        else:
            workflow = image_workflow(params, seed, ref, mask_name)
        return StageResult("submit", {"workflow": workflow})

    async def submit(self, params: GenParams, seed: int, prepared: dict, request_id: str,
                     image: InitImage | None = None, mask: InitImage | None = None) -> StageResult:
        if self.config.provider == "mock":
            generated = await MockProvider().generate(params, seed, lambda *_: None, image, mask)
            return StageResult("persist", {}, generated=generated, finished=True)
        if self.config.provider == "comfyui":
            response = await self.client.post(self.comfy.base + "/prompt", json={
                "prompt": prepared["workflow"], "client_id": request_id})
            response.raise_for_status()
            task_id = response.json()["prompt_id"]
            return StageResult("poll", {}, external_id=task_id)
        if params.kind == "video":
            url, body = self.cloud.video_request(params)
            response = await self.client.post(url, json=body, headers={
                "Authorization": f"Bearer {self.cloud.video_key()}", "X-DashScope-Async": "enable"})
            response.raise_for_status()
            return StageResult("poll", {}, external_id=response.json()["output"]["task_id"])
        url, body = self.cloud.request(params, seed, image, mask)
        response = await self.client.post(url, json=body, headers={
            "Authorization": f"Bearer {self.config.imageApiKey}"})
        response.raise_for_status()
        payload = response.json()
        if self.config.cloudVendor == "aliyun":
            items = [part for choice in payload.get("output", {}).get("choices", [])
                     for part in choice.get("message", {}).get("content", []) if part.get("image")]
            item = {"url": items[0]["image"]} if items else {}
        else:
            item = (payload.get("images") or payload.get("data") or [{}])[0]
        if item.get("b64_json"):
            return StageResult("persist", {}, generated=Generated(
                base64.b64decode(item["b64_json"], validate=True), "png"), finished=True)
        if not item.get("url"):
            raise ValueError("上游响应缺少产物定位信息")
        return StageResult("download", {"url": item["url"]}, finished=True)

    async def poll(self, task_id: str, kind: str) -> StageResult:
        if self.config.provider == "comfyui":
            response = await self.client.get(self.comfy.base + f"/history/{task_id}")
            response.raise_for_status()
            entry = response.json().get(task_id, {})
            if entry.get("status", {}).get("status_str") == "error":
                return StageResult("failed", {"reason": "上游执行失败"}, finished=True)
            files = [f for output in entry.get("outputs", {}).values()
                     for key in ("images", "videos") for f in output.get(key, [])]
            if not files:
                return StageResult("poll", {}, external_id=task_id)
            preferred = {".mp4", ".webm", ".mkv", ".gif"} if kind == "video" else {".png", ".jpg", ".jpeg", ".webp"}
            item = next((f for f in files if PurePosixPath(f["filename"]).suffix.lower() in preferred), files[0])
            return StageResult("download", {"url": self.comfy.base + "/view",
                "params": {k: item.get(k, "") for k in ("filename", "subfolder", "type")},
                "ext": PurePosixPath(item["filename"]).suffix.lstrip(".")}, finished=True)
        output = await self.cloud.video_task(task_id, self.cloud.video_key())
        status = output.get("task_status")
        if status == "SUCCEEDED":
            return StageResult("download", {"url": output["video_url"]}, finished=True)
        if status in {"FAILED", "CANCELED"}:
            return StageResult("failed", {"reason": "上游任务未成功"}, finished=True)
        if status not in {"PENDING", "RUNNING"}:
            raise ValueError("无法确定上游任务状态")
        return StageResult("poll", {}, external_id=task_id)

    async def cancel(self, task_id: str, kind: str) -> StageResult:
        if self.capabilities(kind).cancel:
            response = await self.client.post(self.cloud.vendor_base(f"/tasks/{task_id}/cancel"),
                headers={"Authorization": f"Bearer {self.cloud.video_key()}"})
            response.raise_for_status()
            # An accepted cancellation is not proof that the upstream stopped.
            # Query the same handle independently before releasing occupancy.
        return StageResult("poll", {}, external_id=task_id)
