"""Normalize legacy canvas generation payloads without changing their JSON keys."""

from __future__ import annotations

import base64
import math
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel


@dataclass(frozen=True)
class InitImage:
    data: bytes
    ext: str

    def data_url(self) -> str:
        ext = "jpeg" if self.ext == "jpg" else self.ext
        return f"data:image/{ext};base64,{base64.b64encode(self.data).decode()}"


class GenParams(BaseModel):
    prompt: str
    model: str
    negativePrompt: str = ""
    width: int = 512
    height: int = 512
    steps: int = 20
    cfgScale: float = 7
    seed: int = -1
    batchCount: int = 1
    sampler: str = "euler"
    scheduler: str = "normal"
    denoise: float = 1
    kind: Literal["image", "video"] = "image"
    durationSec: float = 4
    fps: int = 16
    inpaint: bool | None = None
    sourcePrompt: str | None = None
    referenceWeight: Literal["", "low", "medium", "high"] = ""


# 参考强度档位 → denoise 映射;仅 ComfyUI/Mock 消费,云端必须显式拒绝(能力契约 referenceWeightAdjustable)。
REFERENCE_WEIGHT_DENOISE = {"low": 0.4, "medium": 0.6, "high": 0.9}


def with_reference_weight(params: GenParams, has_image: bool) -> GenParams:
    if params.referenceWeight and has_image:
        return params.model_copy(update={"denoise": REFERENCE_WEIGHT_DENOISE[params.referenceWeight]})
    return params


def clamp(raw, low, high, default):
    try:
        value = float(raw)
        return min(high, max(low, value)) if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def parse_params(raw: dict) -> GenParams:
    if not isinstance(raw, dict):
        raise ValueError("请求体必须是 JSON 对象")
    prompt = raw.get("prompt", "")
    model = raw.get("model", "")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("提示词(prompt)不能为空")
    if len(prompt.strip()) > 4000:
        raise ValueError("提示词过长(上限 4000 字符)")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("缺少模型(model)")
    data = {
        "prompt": prompt.strip(),
        "model": model.strip(),
        "kind": "video" if raw.get("kind") == "video" else "image",
    }
    for key, low, high, default in [
        ("width", 64, 2048, 512),
        ("height", 64, 2048, 512),
        ("steps", 1, 150, 20),
        ("cfgScale", 1, 30, 7),
        ("seed", -1, 2**31 - 1, -1),
        ("batchCount", 1, 16, 1),
        ("denoise", 0.05, 1, 1),
        ("durationSec", 1, 12, 4),
        ("fps", 4, 30, 16),
    ]:
        value = clamp(raw.get(key), low, high, default)
        if key in ("width", "height"):
            value = math.floor(value / 8 + 0.5) * 8
        elif key in ("steps", "batchCount", "fps"):
            value = math.floor(value + 0.5)
        elif key == "seed":
            value = math.floor(value)
        data[key] = value
    for key, default in [("negativePrompt", ""), ("sampler", "euler"), ("scheduler", "normal")]:
        value = raw.get(key)
        data[key] = (
            value.strip()[: 4000 if key == "negativePrompt" else 100]
            if isinstance(value, str) and value.strip()
            else default
        )
    weight = raw.get("referenceWeight", "")
    if weight is None or weight == "":
        data["referenceWeight"] = ""
    elif isinstance(weight, str) and weight.strip().lower() in ("low", "medium", "high"):
        data["referenceWeight"] = weight.strip().lower()
    else:
        raise ValueError("参考强度只支持 low/medium/high")
    return GenParams.model_validate(data)


def parse_image(raw, mask=False) -> InitImage | None:
    if raw is None or raw == "":
        return None
    match = (
        re.fullmatch(r"data:image/(png|jpe?g|webp);base64,([A-Za-z0-9+/=\s]+)", raw)
        if isinstance(raw, str)
        else None
    )
    if not match or (mask and match[1] != "png"):
        raise ValueError(
            "蒙版必须是 PNG data URL" if mask else "参考图必须是 PNG/JPEG/WebP data URL"
        )
    try:
        data = base64.b64decode(re.sub(r"\s", "", match[2]), validate=True)
    except ValueError as exc:
        raise ValueError("图片 base64 无效") from exc
    if not data or len(data) > 8 * 1024 * 1024:
        raise ValueError("图片为空或超过 8MB")
    return InitImage(data, "jpeg" if match[1] == "jpg" else match[1])
