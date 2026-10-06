"""Port of the four existing cloud adapters; no additional billable calls."""

import asyncio
import base64

import httpx

from .base import Generated, Provider
from .policy import Operation, ProviderFailure, classify_failure


def resolve_model(config, reference=False):
    if config.cloudVendor == "aliyun" and config.cloudModel == "qwen-image-2.0" and reference:
        return "qwen-image-edit-plus"
    return config.cloudModel


# 通义万相视频:DashScope 异步任务协议(创建返回 task_id → 轮询 /tasks/{id} → 下载 video_url)。
# 协议形状与档位以官方文档与实测为准,未实测前视为待验证(技术方案 v0.1 §5.3)。
VIDEO_SYNTHESIS_PATH = "/services/aigc/video-generation/video-synthesis"
VIDEO_SIZES = {(1280, 720), (720, 1280), (960, 960)}
VIDEO_DURATIONS = (5,)
VIDEO_POLL_SEC = 3


async def chat(client, config, prompt, system, temperature=0, max_tokens=600, timeout=15):
    response = await client.post(
        config.cloudBaseUrl.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {config.imageApiKey}"},
        json={
            "model": config.cloudTextModel,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=timeout,
    )
    response.raise_for_status()
    text = response.json()["choices"][0]["message"]["content"].strip()
    if not text:
        raise ValueError("文本模型返回空内容")
    return text


class CloudProvider(Provider):
    name = "cloud"
    capacity = 2

    def __init__(self, config, client: httpx.AsyncClient):
        self.config = config.model_copy()
        self.client = client

    async def status(self):
        return {
            "ok": bool(self.config.imageApiKey),
            "detail": "云端 API 已配置" if self.config.imageApiKey else "未配置图像 API Key",
        }

    async def models(self):
        return [{"id": self.config.cloudModel, "name": self.config.cloudModel}]

    def vendor_base(self, endpoint):
        base = self.config.cloudBaseUrl.rstrip("/")
        if base.endswith(endpoint):
            return base
        if not base.endswith("/api/v1"):
            base += "/api/v1"
        return base + endpoint

    def video_key(self):
        # DashScope 账号级密钥:视频 Key 优先,未单独配置时回退图像 Key。
        return self.config.videoApiKey or self.config.imageApiKey

    def video_request(self, p):
        c = self.config
        if not c.videoModel:
            raise ValueError("请先在设置中配置视频模型(如 wanx2.1-t2v-turbo)")
        size = f"{p.width}*{p.height}"
        if (p.width, p.height) not in VIDEO_SIZES:
            supported = "、".join(f"{w}*{h}" for w, h in sorted(VIDEO_SIZES, reverse=True))
            raise ValueError(f"不支持的画幅 {size},当前支持:{supported}")
        duration = int(round(p.durationSec))
        if duration not in VIDEO_DURATIONS:
            supported = "、".join(str(x) for x in VIDEO_DURATIONS)
            raise ValueError(f"不支持的时长 {duration} 秒,当前支持:{supported} 秒")
        body = {
            "model": c.videoModel,
            "input": {"prompt": p.prompt},
            "parameters": {"size": size, "duration": duration},
        }
        if p.negativePrompt:
            body["input"]["negative_prompt"] = p.negativePrompt
        return self.vendor_base(VIDEO_SYNTHESIS_PATH), body

    async def video_task(self, task_id, key):
        try:
            response = await self.client.get(
                self.vendor_base(f"/tasks/{task_id}"),
                headers={"Authorization": f"Bearer {key}"},
                timeout=30,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            failure = classify_failure(exc, Operation.POLL)
            failure.message = f"查询视频任务失败:{failure.message}"
            failure.args = (failure.message,)
            raise failure from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        output = payload.get("output") or {}
        if not response.is_success or not output.get("task_status"):
            detail = (
                payload.get("message")
                or payload.get("error", {}).get("message")
                or f"HTTP {response.status_code}"
            )
            raise ValueError(f"查询视频任务失败:{detail}")
        return output

    async def generate_video(self, params, progress, image, mask, external, external_task_id):
        if image or mask:
            raise ValueError("云端图生视频暂未开放:请使用文字生成视频或切换本地后端")
        key = self.video_key()
        if not key:
            raise ValueError("未配置视频 API Key")
        async with asyncio.timeout(self.config.videoTimeoutMin * 60):
            if external_task_id:
                # 重启对账:只查询既有任务,绝不重新提交付费请求(技术方案 v0.1 §5.4)。
                task_id = external_task_id
                progress(0.1, "查询上游任务")
            else:
                url, body = self.video_request(params)
                progress(0.05, "创建视频任务")
                response = await self.client.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {key}", "X-DashScope-Async": "enable"},
                    timeout=30,
                )
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                output = payload.get("output") or {}
                if not response.is_success or not output.get("task_id"):
                    detail = (
                        payload.get("message")
                        or payload.get("error", {}).get("message")
                        or f"HTTP {response.status_code}"
                    )
                    raise ValueError(f"创建视频任务失败:{detail}")
                task_id = output["task_id"]
                if external:
                    external(task_id)
            while True:
                output = await self.video_task(task_id, key)
                status = output.get("task_status")
                if status == "SUCCEEDED":
                    video_url = output.get("video_url")
                    if not video_url:
                        raise ValueError("云端视频任务成功但缺少视频地址")
                    progress(0.85, "下载视频")
                    download = await self.client.get(video_url, timeout=120)
                    download.raise_for_status()
                    if not download.content:
                        raise ValueError("云端返回空视频")
                    mime = download.headers.get("content-type", "")
                    ext = "webm" if "webm" in mime else "mp4"
                    return Generated(download.content, ext)
                if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                    detail = output.get("message") or f"上游状态 {status}"
                    raise ValueError(f"云端视频任务未成功:{detail}")
                progress(
                    0.4 if status == "RUNNING" else 0.15,
                    "生成中" if status == "RUNNING" else "排队中",
                )
                await asyncio.sleep(VIDEO_POLL_SEC)

    async def query_external(self, task_id):
        key = self.video_key()
        if not key:
            raise ValueError("未配置视频 API Key")
        return await self.video_task(task_id, key)

    async def cancel_external(self, task_id):
        key = self.video_key()
        if not key:
            return False
        try:
            response = await self.client.post(
                self.vendor_base(f"/tasks/{task_id}/cancel"),
                headers={"Authorization": f"Bearer {key}"},
                timeout=15,
            )
            payload = response.json()
            return response.is_success and (
                (payload.get("output") or {}).get("task_status") == "CANCELED"
            )
        except Exception:
            return False

    def request(self, p, seed, image, mask):
        c = self.config
        base = c.cloudBaseUrl.rstrip("/")
        vendor = c.cloudVendor
        if image and vendor not in {"aliyun", "siliconflow"}:
            raise ValueError(f"当前厂商({vendor})暂不支持参考图")
        if mask and vendor != "siliconflow":
            raise ValueError(f"当前厂商({vendor})暂不支持蒙版局部重绘")
        if vendor == "aliyun":
            if not 512**2 <= p.width * p.height <= 2048**2:
                raise ValueError("百炼尺寸总像素需在 512×512 至 2048×2048 之间")
            endpoint = "/services/aigc/multimodal-generation/generation"
            url = (
                base
                if base.endswith(endpoint)
                else (base if base.endswith("/api/v1") else base + "/api/v1") + endpoint
            )
            content = ([{"image": image.data_url()}] if image else []) + [{"text": p.prompt}]
            return url, {
                "model": resolve_model(c, bool(image)),
                "input": {"messages": [{"role": "user", "content": content}]},
                "parameters": {
                    "size": f"{p.width}*{p.height}",
                    "n": 1,
                    "prompt_extend": False,
                    "watermark": False,
                    "negative_prompt": p.negativePrompt,
                    "seed": seed,
                },
            }
        body = {"model": c.cloudModel, "prompt": p.prompt}
        if vendor == "siliconflow":
            body.update(image_size=f"{p.width}x{p.height}", batch_size=1)
            if image:
                body.update(model="black-forest-labs/FLUX.1-Kontext-dev", image=image.data_url())
            if mask:
                body.update(model="black-forest-labs/FLUX.1-Fill-dev", image_mask=mask.data_url())
        else:
            body["size"] = f"{p.width}x{p.height}"
            if vendor == "openai":
                body["n"] = 1
        return base + "/images/generations", body

    async def generate(
        self, params, seed, progress, image=None, mask=None, external=None, external_task_id=None
    ):
        key = self.config.imageApiKey
        operation = Operation.SUBMIT
        try:
            if params.kind == "video":
                return await self.generate_video(
                    params, progress, image, mask, external, external_task_id
                )
            if not key:
                raise ValueError("未配置图像 API Key")
            url, body = self.request(params, seed, image, mask)
            progress(0.1, "请求云端生成")
            response = await self.client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {key}"},
                timeout=self.config.imageTimeoutMin * 60,
            )
            response.raise_for_status()
            payload = response.json()
            if not response.is_success or payload.get("code"):
                detail = (
                    payload.get("error", {}).get("message")
                    or payload.get("message")
                    or "云端生成失败"
                )
                raise ValueError(f"HTTP {response.status_code}: {detail}")
            if self.config.cloudVendor == "aliyun":
                items = [
                    x
                    for choice in payload.get("output", {}).get("choices", [])
                    for x in choice.get("message", {}).get("content", [])
                    if x.get("image")
                ]
                item = {"url": items[0]["image"]} if items else {}
            else:
                items = payload.get("images") or payload.get("data") or [{}]
                item = items[0]
            if item.get("b64_json"):
                data = base64.b64decode(item["b64_json"], validate=True)
                ext = "png"
            elif item.get("url"):
                operation = Operation.DOWNLOAD
                progress(0.75, "下载图像")
                download = await self.client.get(item["url"], timeout=60)
                download.raise_for_status()
                data = download.content
                mime = download.headers.get("content-type", "")
                ext = "jpg" if "jpeg" in mime else "webp" if "webp" in mime else "png"
            else:
                raise ValueError("云端响应缺少图像 url/b64")
            if not data:
                raise ValueError("云端返回空图像")
            return Generated(data, ext)
        except ProviderFailure:
            raise
        except (httpx.HTTPError, TimeoutError) as exc:
            raise classify_failure(exc, operation) from exc
        except Exception as exc:
            message = str(exc)
            redact = key or self.video_key()
            if redact:
                message = message.replace(redact, "[REDACTED]")
            raise ValueError(message) from None
