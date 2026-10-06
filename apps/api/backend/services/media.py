"""Shared media validation and immutable publication, with no mutable JSON index."""
import asyncio
import io
from pathlib import Path
import uuid
import wave

from PIL import Image, UnidentifiedImageError
from fastapi import HTTPException

from backend.infrastructure.artifacts import ArtifactSpool
from backend.infrastructure.orm import Asset
from backend.providers.base import Generated


def inspect_media(data: bytes, kind: str, ext: str = "") -> dict:
    if not data:
        raise ValueError("Empty media")
    result = {"width": 0, "height": 0, "details": {}}
    if kind == "image":
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 32_000_000 or image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Unsupported image")
            result.update(width=image.width, height=image.height,
                          ext={"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[image.format])
            image.verify()
    elif kind == "audio":
        with wave.open(io.BytesIO(data)) as audio:
            frames, rate = audio.getnframes(), audio.getframerate()
            channels, width = audio.getnchannels(), audio.getsampwidth()
            if not (1 <= channels <= 2 and width in {1, 2, 3, 4} and 8000 <= rate <= 192000 and 0 < frames / rate <= 3600):
                raise ValueError("Unsupported PCM WAV")
            if len(audio.readframes(frames)) != frames * channels * width:
                raise ValueError("Truncated WAV")
            result.update(ext="wav", details={"durationSec": frames / rate, "channels": channels, "sampleRate": rate})
    elif kind == "video":
        if ext == "mp4" and data[4:8] == b"ftyp":
            result["ext"] = "mp4"
        elif ext == "webm" and data[:4] == b"\x1a\x45\xdf\xa3":
            result["ext"] = "webm"
        else:
            raise ValueError("Unsupported video container")
    else:
        raise ValueError("Unsupported media kind")
    return result


async def save_media(factory, root: Path, workspace: uuid.UUID, data: bytes,
                     kind="image", ext="", name="") -> Asset:
    try:
        meta = await asyncio.to_thread(inspect_media, data, kind, ext)
    except (UnidentifiedImageError, wave.Error, EOFError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid media") from exc
    ident = uuid.uuid4()
    try:
        artifact = await asyncio.to_thread(ArtifactSpool(root).save, ident, 0, Generated(data, meta["ext"]))
    except OSError as exc:
        raise HTTPException(503, "媒体存储暂不可用") from exc
    async with factory.begin() as session:
        row = Asset(id=ident, workspace_id=workspace, kind=kind, ext=meta["ext"],
            width=meta["width"], height=meta["height"], bytes=len(data), sha256=artifact["sha256"],
            storage_key=artifact["storage_key"], details={**meta["details"], "name": name[:200]})
        session.add(row)
        await session.flush()
        return row
