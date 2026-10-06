import asyncio
from pathlib import PurePosixPath
from uuid import uuid4

from ..models import with_reference_weight
from .base import Generated, Provider
from .workflows import image_workflow, ltx_workflow, wan_workflow


class ComfyProvider(Provider):
    name = "comfyui"

    def __init__(self, config, client):
        self.config = config.model_copy()
        self.client = client
        self.base = config.comfyUrl.rstrip("/")

    async def json(self, path, method="GET", **kwargs):
        response = await self.client.request(method, self.base + path, timeout=10, **kwargs)
        response.raise_for_status()
        return response.json()

    async def status(self):
        try:
            await asyncio.wait_for(self.json("/system_stats"), 2.5)
            return {"ok": True, "detail": f"ComfyUI @ {self.base}"}
        except Exception:
            return {"ok": False, "detail": "无法连接 ComfyUI,请先启动 ComfyUI"}

    async def choices(self, node, field):
        info = await self.json(f"/object_info/{node}")
        return info.get(node, {}).get("input", {}).get("required", {}).get(field, [[]])[0]

    async def models(self):
        return [
            {"id": n, "name": n} for n in await self.choices("CheckpointLoaderSimple", "ckpt_name")
        ]

    async def samplers(self):
        return {
            "samplers": await self.choices("KSampler", "sampler_name"),
            "schedulers": await self.choices("KSampler", "scheduler"),
        }

    async def upload(self, image):
        result = await self.json(
            "/upload/image",
            "POST",
            files={"image": (f"ref-{uuid4()}.{image.ext}", image.data, f"image/{image.ext}")},
            data={"overwrite": "true"},
        )
        if not result.get("name"):
            raise ValueError("ComfyUI 未返回上传文件名")
        return result["name"]

    async def video_model(self):
        c = self.config
        if c.wanHighNoiseUnet and c.wanLowNoiseUnet:
            return "wan", c.wanHighNoiseUnet, c.wanLowNoiseUnet
        if c.videoModel:
            backend = (
                c.videoBackend
                if c.videoBackend != "auto"
                else ("wan" if "wan" in c.videoModel.lower() else "ltxv")
            )
            return backend, c.videoModel, None
        checkpoints = await self.choices("CheckpointLoaderSimple", "ckpt_name")
        unets = await self.choices("UNETLoader", "unet_name")
        ltx = next((x for x in checkpoints if "ltx" in x.lower()), None)
        wan = next((x for x in unets if "wan" in x.lower() and "i2v" in x.lower()), None) or next(
            (x for x in unets if "wan" in x.lower()), None
        )
        if wan and c.videoBackend != "ltxv":
            return "wan", wan, None
        if ltx and c.videoBackend != "wan":
            return "ltxv", ltx, None
        raise ValueError("未找到可用的视频模型,请配置 videoModel")

    async def generate(
        self, params, seed, progress, image=None, mask=None, external=None, external_task_id=None
    ):
        c = self.config
        video = params.kind == "video"
        params = with_reference_weight(params, image is not None)
        prompt_id = None
        try:
            async with asyncio.timeout((c.videoTimeoutMin if video else c.imageTimeoutMin) * 60):
                ref = await self.upload(image) if image else None
                mask_name = await self.upload(mask) if image and mask else None
                if video:
                    backend, model, low = await self.video_model()
                    workflow = (
                        ltx_workflow(params, seed, model, ref)
                        if backend == "ltxv"
                        else wan_workflow(params, seed, model, c.wanClip, c.wanVae, ref, low)
                    )
                else:
                    workflow = image_workflow(params, seed, ref, mask_name)
                result = await self.json(
                    "/prompt", "POST", json={"prompt": workflow, "client_id": str(uuid4())}
                )
                prompt_id = result.get("prompt_id")
                if not prompt_id:
                    raise ValueError("ComfyUI 未返回任务 ID")
                progress(0.05, "ComfyUI 生成中")
                while True:
                    await asyncio.sleep(0.7)
                    history = await self.json(f"/history/{prompt_id}")
                    entry = history.get(prompt_id, {})
                    if entry.get("status", {}).get("status_str") == "error":
                        raise ValueError("ComfyUI 执行出错,请查看其日志")
                    files = [
                        f
                        for output in entry.get("outputs", {}).values()
                        for key in ("images", "videos")
                        for f in output.get(key, [])
                    ]
                    if files:
                        preferred = (
                            {".mp4", ".webm", ".mkv", ".gif"}
                            if video
                            else {".png", ".jpg", ".jpeg", ".webp"}
                        )
                        out = next(
                            (
                                f
                                for f in files
                                if PurePosixPath(f["filename"]).suffix.lower() in preferred
                            ),
                            files[0],
                        )
                        response = await self.client.get(
                            self.base + "/view",
                            params={k: out.get(k, "") for k in ("filename", "subfolder", "type")},
                            timeout=60,
                        )
                        response.raise_for_status()
                        if not response.content:
                            raise ValueError("ComfyUI 返回空文件")
                        return Generated(
                            response.content,
                            PurePosixPath(out["filename"]).suffix.lstrip(".").lower(),
                        )
        except (asyncio.CancelledError, TimeoutError):
            if prompt_id:
                # Remove only our queued prompt; interrupt only when ours is currently running.
                try:
                    await self.json("/queue", "POST", json={"delete": [prompt_id]})
                    queue = await self.json("/queue")
                    if any(
                        len(item) > 1 and item[1] == prompt_id
                        for item in queue.get("queue_running", [])
                    ):
                        await self.json("/interrupt", "POST")
                except Exception:
                    pass
            raise
