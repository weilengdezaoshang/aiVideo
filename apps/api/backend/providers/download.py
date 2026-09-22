"""Bounded streaming download with an explicit origin allowlist and no redirects.

Credentials and their headers are never forwarded to artifact origins. Production
must list CDN origins explicitly; redirects must be resolved by a trusted adapter.
"""

import httpx

from backend.providers.base import Generated


async def download_artifact(client, payload: dict, kind: str, allowed_origins: set[str], *, internal=False) -> Generated:
    url = httpx.URL(payload["url"])
    if url.scheme not in {"http", "https"} or url.userinfo:
        raise ValueError("invalid artifact URL")
    if not internal and url.scheme != "https":
        raise ValueError("public artifacts require HTTPS")
    origin = str(url.copy_with(path="/", query=None, fragment=None)).rstrip("/")
    if origin not in allowed_origins:
        raise ValueError("artifact origin is not configured")
    maximum = 512 * 1024**2 if kind == "video" else 32 * 1024**2
    data = bytearray()
    async with client.stream("GET", url, params=payload.get("params"), follow_redirects=False) as response:
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and int(declared) > maximum:
            raise ValueError("artifact exceeds size limit")
        mime = response.headers.get("content-type", "").split(";")[0]
        mapping = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                   "image/gif": "gif", "video/mp4": "mp4", "video/webm": "webm",
                   "video/x-matroska": "mkv"}
        ext = mapping.get(mime)
        if ext is None or not mime.startswith("video/" if kind == "video" else "image/"):
            raise ValueError("unexpected artifact content type")
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > maximum:
                raise ValueError("artifact exceeds size limit")
    if not data:
        raise ValueError("empty artifact")
    return Generated(bytes(data), ext)
