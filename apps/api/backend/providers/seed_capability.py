"""Whether a real request body carries seed. Derived from the builders, not from names."""
from __future__ import annotations


def cloud_image_request_includes_seed(vendor: str) -> bool:
    """cloud.CloudProvider.request puts seed only on the Aliyun image parameters object."""
    return vendor == "aliyun"


def cloud_video_request_includes_seed() -> bool:
    """cloud.CloudProvider.video_request has no seed field."""
    return False


def provider_honors_seed(provider: str, kind: str, cloud_vendor: str | None = None) -> bool:
    """Mock render and ComfyUI workflows take seed. Cloud video and non-Aliyun images do not."""
    if provider == "mock":
        return True
    if provider == "comfyui":
        return True
    if provider == "cloud" and kind == "image":
        return cloud_image_request_includes_seed(cloud_vendor or "")
    if provider == "cloud" and kind == "video":
        return cloud_video_request_includes_seed()
    return False
