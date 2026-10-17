"""Owned keyframes become an actual interpolated MP4 in the existing export Worker pool."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import shutil

from PIL import Image, ImageOps

from backend.exports import Exports


async def render_frames(root: Path, claim, binary: str) -> Path:
    sources = claim.params.get("frameSources", [])
    if not 2 <= len(sources) <= 16:
        raise ValueError("frames_to_video requires 2–16 owned keyframes")
    width, height = int(claim.params["width"]), int(claim.params["height"])
    fps, duration = int(claim.params["fps"]), float(claim.params["durationSec"])
    if not (64 <= width <= 2048 and 64 <= height <= 2048 and width % 2 == height % 2 == 0
            and 4 <= fps <= 30 and 1 <= duration <= 12):
        raise ValueError("Invalid frame composition specification")
    work = root / "export-work" / str(claim.job_id) / str(claim.epoch)
    work.mkdir(parents=True, exist_ok=True)

    def prepare():
        signatures = set()
        last = None
        for index, source in enumerate(sources):
            path = (root / source["key"]).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError("Frame source is outside managed media")
            if path.stat().st_size > 32 * 1024**2:
                raise ValueError("Frame source exceeds the decoding limit")
            with Image.open(path) as image:
                if image.width * image.height > 40_000_000:
                    raise ValueError("Frame exceeds the pixel limit")
                frame = ImageOps.pad(image.convert("RGB"), (width, height), color="black")
                signatures.add(hashlib.sha256(frame.tobytes()).digest())
                frame.save(work / f"frame-{index:03d}.png")
                last = index
        if len(signatures) < 2:
            raise ValueError("static_frames: identical images cannot satisfy a motion video goal")
        # Optical flow needs neighboring frames at both ends; endpoint padding is explicit.
        for index in range(len(sources), len(sources) + 2):
            shutil.copyfile(work / f"frame-{last:03d}.png", work / f"frame-{index:03d}.png")
    await asyncio.to_thread(prepare)
    output = work / "output.mp4"
    frame_rate = (len(sources) - 1) / duration
    renderer = Exports(root, durable=True, work_root=work)
    await renderer.command([binary, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-filter_threads", "1", "-framerate", str(frame_rate),
        "-i", str(work / "frame-%03d.png"), "-vf",
        f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1",
        "-t", str(duration), "-r", str(fps), "-an", "-c:v", "libx264", "-threads", "1",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)])
    if not output.is_file() or output.stat().st_size < 100:
        raise ValueError("Frame composition produced no playable video")
    return output
