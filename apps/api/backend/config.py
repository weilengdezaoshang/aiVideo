"""Existing config.json/SWARMUI_* contract, with masked public credentials."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .common import read_json, write_json

VENDORS = {
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "cogview-4-250304"),
    "siliconflow": ("https://api.siliconflow.cn/v1", "black-forest-labs/FLUX.1-schnell"),
    "openai": ("https://api.openai.com/v1", "gpt-image-1"),
    "aliyun": ("https://dashscope.aliyuncs.com/api/v1", "qwen-image-2.0"),
}
ENV = {
    "provider": "SWARMUI_PROVIDER",
    "comfyUrl": "SWARMUI_COMFY_URL",
    "cloudVendor": "SWARMUI_CLOUD_VENDOR",
    "cloudBaseUrl": "SWARMUI_CLOUD_BASE_URL",
    "cloudModel": "SWARMUI_CLOUD_MODEL",
    "cloudTextModel": "SWARMUI_CLOUD_TEXT_MODEL",
    "port": "SWARMUI_PORT",
    "imageTimeoutMin": "SWARMUI_IMAGE_TIMEOUT_MIN",
    "videoTimeoutMin": "SWARMUI_VIDEO_TIMEOUT_MIN",
    "videoModel": "SWARMUI_VIDEO_MODEL",
    "videoBackend": "SWARMUI_VIDEO_BACKEND",
    "wanClip": "SWARMUI_WAN_CLIP",
    "wanVae": "SWARMUI_WAN_VAE",
    "wanHighNoiseUnet": "SWARMUI_WAN_HIGH_NOISE_UNET",
    "wanLowNoiseUnet": "SWARMUI_WAN_LOW_NOISE_UNET",
    "imageApiKey": "SWARMUI_IMAGE_API_KEY",
    "videoApiKey": "SWARMUI_VIDEO_API_KEY",
}
PATCHABLE = {
    "provider",
    "comfyUrl",
    "cloudVendor",
    "cloudBaseUrl",
    "cloudModel",
    "videoModel",
    "cloudTextModel",
    "imageApiKey",
    "videoApiKey",
}


class Config(BaseModel):
    provider: Literal["mock", "comfyui", "cloud"] = "mock"
    comfyUrl: str = "http://127.0.0.1:8188"
    cloudVendor: Literal["zhipu", "siliconflow", "openai", "aliyun"] = "zhipu"
    cloudBaseUrl: str = ""
    cloudModel: str = ""
    cloudTextModel: str = ""
    port: int = Field(default=7801, ge=1, le=65535)
    imageTimeoutMin: float = Field(default=20, ge=1, le=240)
    videoTimeoutMin: float = Field(default=60, ge=1, le=240)
    videoModel: str = ""
    videoBackend: Literal["auto", "ltxv", "wan"] = "auto"
    wanClip: str = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
    wanVae: str = "wan_2.1_vae.safetensors"
    wanHighNoiseUnet: str = ""
    wanLowNoiseUnet: str = ""
    imageApiKey: str = Field(default="", repr=False)
    videoApiKey: str = Field(default="", repr=False)


def normalize(data: dict) -> Config:
    try:
        config = Config.model_validate(data)
    except ValueError as exc:
        raise ValueError("配置字段类型或取值不正确") from exc
    base, model = VENDORS[config.cloudVendor]
    config.cloudBaseUrl = config.cloudBaseUrl or base
    config.cloudModel = config.cloudModel or model
    for key in ("comfyUrl", "cloudBaseUrl"):
        if not getattr(config, key).startswith(("http://", "https://")):
            raise ValueError(f"配置错误:{key} 必须以 http:// 或 https:// 开头")
    return config


def load_config(root: Path) -> Config:
    # SWARMUI_CONFIG 允许隔离实例（如 SWARMUI_DATA_DIR 指向测试目录）使用独立配置，
    # 避免测试实例误读仓库 config.json 中的真实密钥。
    override = os.environ.get("SWARMUI_CONFIG", "").strip()
    path = Path(override) if override else root / "config.json"
    data = read_json(path, {})
    for field, env in ENV.items():
        if os.environ.get(env, "").strip():
            data[field] = os.environ[env].strip()
    return normalize(data)


def patch_config(current: Config, patch: dict, root: Path | None = None):
    changes = {}
    for key, value in patch.items():
        if key not in PATCHABLE:
            continue
        if not isinstance(value, str):
            raise ValueError(f"配置错误:{key} 必须是字符串")
        value = value.strip()
        if len(value) > (4096 if key.endswith("ApiKey") else 2000):
            raise ValueError(f"配置错误:{key} 过长")
        changes[key] = value
    candidate = normalize({**current.model_dump(), **changes})
    overridden = []
    if root is not None:
        write_json(root / "config.json", {**read_json(root / "config.json", {}), **changes})
        # Persist desired values, but environment always wins during this run and next boot.
        effective = candidate.model_dump()
        for key in changes:
            if os.environ.get(ENV[key], "").strip():
                overridden.append(key)
                effective[key] = os.environ[ENV[key]].strip()
        candidate = normalize(effective)
    return candidate, overridden


def public_config(config: Config) -> dict:
    result = {key: getattr(config, key) for key in PATCHABLE if not key.endswith("ApiKey")}
    for kind in ("image", "video"):
        key = getattr(config, f"{kind}ApiKey")
        result[f"{kind}ApiKeyMasked"] = (
            ("••••" + (key[-4:] if len(key) > 4 else "")) if key else None
        )
        result[f"has{kind.title()}Key"] = bool(key)
    return result
