"""Read-only font whitelist. Bundled faces are public; a custom registry requires a session.

The index lists id, version and sha256 only. Bytes are selected by that pair.
Hashing runs off the event loop and stops at max_font_bytes. No path is returned.
"""

from __future__ import annotations

import asyncio
import hashlib

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from backend.core.tools.contracts import ToolSettings

from .errors import CompositionError
from .fonts import load_registry


def _digest(path, limit: int) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    size = path.stat().st_size
    if size < 1 or size > limit:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def install(app: FastAPI) -> None:
    @app.get("/api/fonts")
    async def font_index(request: Request):
        settings, registry = await _open(request)
        return {"fonts": registry.public_catalog(), "max_bytes": settings.max_font_bytes}

    @app.get("/api/fonts/{font_id}")
    async def font_file(font_id: str, sha256: str, request: Request):
        settings, registry = await _open(request)
        try:
            face = registry.get(font_id)
        except CompositionError as exc:
            raise HTTPException(404, "字体不存在") from exc
        if sha256 != face.sha256:
            raise HTTPException(404, "字体不存在")
        digest = await asyncio.to_thread(_digest, face.path, settings.max_font_bytes)
        if digest != face.sha256:
            raise HTTPException(404, "字体不存在")
        return FileResponse(face.path, media_type="font/otf", filename=f"{face.id}.otf",
                            headers={"Cache-Control": "public, max-age=31536000, immutable",
                                     "ETag": f'"{face.sha256}"'})


async def _open(request: Request):
    settings = ToolSettings()
    if settings.font_registry_path:
        from backend.services.identity import authenticated
        await authenticated(request)
    try:
        registry = load_registry(settings.font_registry_path, settings.max_font_bytes)
    except (CompositionError, OSError, ValueError) as exc:
        raise HTTPException(404, "字体不存在") from exc
    return settings, registry
