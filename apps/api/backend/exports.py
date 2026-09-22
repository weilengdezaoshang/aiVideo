"""Persisted snapshot exports. FFmpeg only receives copied, local managed media."""

import asyncio
import hashlib
import json
import os
import re
import signal
import shutil
import wave
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .common import now, read_json, write_json
from .timeline import validate_timeline, timeline_srt


def ffmpeg_binary():
    installed = shutil.which("ffmpeg")
    if installed:
        return installed
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        raise ValueError("未安装视频导出运行时，请安装 imageio-ffmpeg 或 FFmpeg") from None


def source_path(data: Path, url: str):
    # Never pass arbitrary URLs, traversal, playlists or user-selected filesystem paths to FFmpeg.
    asset = re.fullmatch(r"/assets/([0-9a-f-]{36})/original\.(png|jpg|jpeg|webp|mp4|webm|wav)", url)
    history = re.fullmatch(r"/images/(img_[0-9a-f]{8,}\.(?:png|jpg|jpeg|webp|mp4|webm))", url)
    if asset:
        path = data / "assets" / asset[1] / f"original.{asset[2]}"
    elif history:
        path = data / "images" / history[1]
    else:
        raise ValueError("导出素材须先导入资产库；不支持外部链接或临时图片地址")
    if not path.is_file() or not path.resolve().is_relative_to(data.resolve()):
        raise ValueError("导出素材已丢失，请重新导入")
    return path


class Exports:
    def __init__(self, data, *, durable=False, source_resolver=None, work_root=None):
        self.data = data
        self.root = work_root or data / "exports"
        self.durable = durable
        self.resolve_source = source_resolver or (lambda url: source_path(self.data, url))
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.tasks = {}
        self.slot = asyncio.Semaphore(1)
        for path in (() if durable else self.root.glob("*/job.json")):
            job = read_json(path, None)
            if isinstance(job, dict) and job.get("id"):
                self.jobs[job["id"]] = job
                if job["status"] in ("queued", "running"):
                    job.update(status="failed", error="服务重启中断了导出，请重新导出")
                    self.save(job)

    def save(self, job):
        job["updatedAt"] = now()
        if not self.durable:
            write_json(self.root / job["id"] / "job.json", job)

    def submit(self, ident, doc_id, raw):
        try:
            if str(UUID(ident)) != ident:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("导出请求标识无效") from None
        timeline = validate_timeline(raw)
        fingerprint = hashlib.sha256(
            json.dumps([doc_id, timeline], sort_keys=True).encode()
        ).hexdigest()
        if ident in self.jobs:
            job = self.jobs[ident]
            if job["fingerprint"] != fingerprint:
                raise HTTPException(409, "相同导出标识不能用于不同时间线")
            return job
        if not timeline["clips"]:
            raise ValueError("时间线为空，请先加入素材")
        if (
            len(timeline["clips"]) > 200
            or sum(c["outFrame"] - c["inFrame"] for c in timeline["clips"]) > 108000
        ):
            raise ValueError("单次导出支持最多 200 个片段、总时长 1 小时")
        if any(timeline[k] % 2 for k in ("width", "height")):
            raise ValueError("导出画幅的宽高必须为偶数")
        if sum(j["status"] in ("queued", "running") for j in self.jobs.values()) >= 5:
            raise HTTPException(429, "导出队列已满，请稍后重试")
        binary = ffmpeg_binary()
        # Validate every source before accepting; copy under the render slot to avoid blocking the API.
        for clip in timeline["clips"] + timeline.get("audioClips", []):
            source_path(self.data, clip["source"]["url"])
        job = dict(
            id=ident,
            documentId=doc_id,
            fingerprint=fingerprint,
            status="queued",
            progress=0,
            createdAt=now(),
            timeline=timeline,
            error=None,
        )
        self.jobs[ident] = job
        self.save(job)
        self.tasks[ident] = asyncio.create_task(self.render(job, binary))
        return job

    async def command(self, args):
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=1800)
        except BaseException:
            if proc.returncode is None:
                try:
                    if os.name == "posix":
                        os.killpg(proc.pid, signal.SIGTERM)
                    else:
                        proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=10)
                except TimeoutError:
                    try:
                        if os.name == "posix":
                            os.killpg(proc.pid, signal.SIGKILL)
                        else:
                            proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.communicate()
            raise
        if proc.returncode:
            raise ValueError("视频渲染失败，请检查素材格式和实际时长")
        return out.decode(errors="replace"), err.decode(errors="replace")

    async def render(self, job, binary):
        directory = self.root / job["id"]
        directory.mkdir(parents=True, exist_ok=True)
        scratch = directory / "work"
        try:
            async with self.slot:
                job.update(status="running")
                self.save(job)
                scratch.mkdir(exist_ok=True)
                tl = job["timeline"]
                parts = []
                for i, clip in enumerate(tl["clips"]):
                    src = self.resolve_source(clip["source"]["url"])
                    local = scratch / f"input-{i}{src.suffix}"
                    # A worker thread copy cannot be force-stopped; wait for it before cleanup on cancel.
                    copy = asyncio.create_task(asyncio.to_thread(shutil.copyfile, src, local))
                    try:
                        await asyncio.shield(copy)
                    except asyncio.CancelledError:
                        await copy
                        raise
                    count = clip["outFrame"] - clip["inFrame"]
                    seconds = count / 30
                    image = clip["source"]["kind"] == "image"
                    # Media stream inspection uses the same local file, with a bounded process.
                    demux = "mov" if local.suffix == ".mp4" else "matroska"
                    safe_input = ["-protocol_whitelist", "file,pipe"] + (
                        [] if image else ["-f", demux]
                    )
                    has_audio = False
                    if not image:
                        probe = await self.command(
                            [
                                binary,
                                "-v",
                                "info",
                                *safe_input,
                                "-i",
                                str(local),
                                "-t",
                                "0",
                                "-f",
                                "null",
                                "-",
                            ]
                        )
                        has_audio = "Audio:" in probe[1]
                    inputs = (
                        safe_input
                        + (["-loop", "1"] if image else ["-ss", str(clip["inFrame"] / 30)])
                        + [
                            "-i",
                            str(local),
                        ]
                    )
                    filters = f"fps=30,scale={tl['width']}:{tl['height']}:force_original_aspect_ratio=decrease,pad={tl['width']}:{tl['height']}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
                    audio_filters = ["apad", f"volume={clip.get('volume', 1)}"]
                    for field, direction in (("fadeInFrames", "in"), ("fadeOutFrames", "out")):
                        fade = min(clip.get(field, 0), count) / 30
                        if fade:
                            start = 0 if direction == "in" else seconds - fade
                            audio_filters.append(f"afade=t={direction}:st={start}:d={fade}")
                    part = scratch / f"part-{i}.mov"
                    out, _ = await self.command(
                        [
                            binary,
                            "-y",
                            "-v",
                            "error",
                            *inputs,
                            "-f",
                            "lavfi",
                            "-i",
                            "anullsrc=r=48000:cl=stereo",
                            "-map",
                            "0:v:0",
                            "-map",
                            "0:a:0" if has_audio else "1:a:0",
                            "-vf",
                            filters,
                            "-af",
                            ",".join(audio_filters),
                            "-t",
                            str(seconds),
                            "-frames:v",
                            str(count),
                            "-c:v",
                            "libx264",
                            "-preset",
                            "veryfast",
                            "-pix_fmt",
                            "yuv420p",
                            "-c:a",
                            "pcm_s16le",
                            "-ar",
                            "48000",
                            "-ac",
                            "2",
                            "-progress",
                            "pipe:1",
                            "-nostats",
                            str(part),
                        ]
                    )
                    frames = re.findall(r"(?m)^frame=(\d+)", out)
                    if not frames or int(frames[-1]) != count:
                        raise ValueError("视频实际时长不足，无法按指定入出点导出，请重新裁剪")
                    parts.append(part)
                    job["progress"] = round((i + 1) / len(tl["clips"]) * 90)
                    self.save(job)
                manifest = scratch / "concat.txt"
                manifest.write_text("".join(f"file '{p.name}'\n" for p in parts))
                additional_inputs = []
                audio_filters = []
                audio_labels = ["[0:a:0]"]
                for index, audio in enumerate(tl.get("audioClips", []), 1):
                    source = self.resolve_source(audio["source"]["url"])
                    local = scratch / f"audio-{index}.wav"
                    copying = asyncio.create_task(asyncio.to_thread(shutil.copyfile, source, local))
                    try:
                        await asyncio.shield(copying)
                    except asyncio.CancelledError:
                        await copying
                        raise
                    with wave.open(str(local), "rb") as wav:
                        if audio["outFrame"] / 30 > wav.getnframes() / wav.getframerate():
                            raise ValueError("音频实际时长不足，请重新裁剪")
                    additional_inputs += [
                        "-protocol_whitelist",
                        "file,pipe",
                        "-f",
                        "wav",
                        "-i",
                        str(local),
                    ]
                    duration = (audio["outFrame"] - audio["inFrame"]) / 30
                    filters = [
                        f"atrim=start={audio['inFrame'] / 30}:end={audio['outFrame'] / 30}",
                        "asetpts=PTS-STARTPTS",
                        "aresample=48000",
                        f"volume={audio['volume']}",
                    ]
                    for field, direction in (("fadeInFrames", "in"), ("fadeOutFrames", "out")):
                        fade = min(audio[field] / 30, duration)
                        if fade:
                            filters.append(
                                f"afade=t={direction}:st={0 if direction == 'in' else duration - fade}:d={fade}"
                            )
                    filters.append(f"adelay={audio['startFrame'] * 1600}S:all=1")
                    label = f"[audio{index}]"
                    audio_filters.append(f"[{index}:a:0]" + ",".join(filters) + label)
                    audio_labels.append(label)
                mapping = ["-map", "0:v:0"]
                if audio_filters:
                    audio_filters.append(
                        "".join(audio_labels)
                        + f"amix=inputs={len(audio_labels)}:duration=first:normalize=0,alimiter=limit=0.95:level=0:latency=1[mix]"
                    )
                    mapping += ["-filter_complex", ";".join(audio_filters), "-map", "[mix]"]
                else:
                    mapping += ["-map", "0:a:0"]
                captions = timeline_srt(tl)
                if captions:
                    subtitle_file = scratch / "subtitles.srt"
                    subtitle_file.write_text(captions, encoding="utf-8")
                    additional_inputs += ["-f", "srt", "-i", str(subtitle_file)]
                    mapping += [
                        "-map",
                        f"{len(audio_labels)}:s:0",
                        "-c:s",
                        "mov_text",
                        "-metadata:s:s:0",
                        "title=字幕",
                        "-disposition:s:0",
                        "default",
                    ]
                staging = directory / "output.partial.mp4"
                await self.command(
                    [
                        binary,
                        "-y",
                        "-v",
                        "error",
                        "-f",
                        "concat",
                        "-safe",
                        "1",
                        "-i",
                        str(manifest),
                        *additional_inputs,
                        *mapping,
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-t",
                        str(sum(c["outFrame"] - c["inFrame"] for c in tl["clips"]) / 30),
                        "-movflags",
                        "+faststart",
                        str(staging),
                    ]
                )
                staging.replace(directory / "output.mp4")
                job.update(status="completed", progress=100)
                self.save(job)
        except asyncio.CancelledError:
            job.update(status="cancelled", error="导出已取消")
            self.save(job)
        except Exception as error:
            job.update(
                status="failed",
                error=str(error) if isinstance(error, ValueError) else "导出失败，请重试",
            )
            self.save(job)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            (directory / "output.partial.mp4").unlink(missing_ok=True)
            self.tasks.pop(job["id"], None)

    async def cancel(self, ident):
        job = self.jobs.get(ident)
        if job is None:
            raise HTTPException(404, "导出任务不存在")
        task = self.tasks.get(ident)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                job.update(status="cancelled", error="导出已取消")
                self.save(job)
                self.tasks.pop(ident, None)
        return job

    async def close(self):
        for ident in list(self.tasks):
            await self.cancel(ident)


def export_router():
    router = APIRouter()

    @router.post("/api/exports", status_code=202)
    async def create(request: Request):
        raw = await request.json()
        if not isinstance(raw, dict) or not request.app.state.documents.get(
            raw.get("documentId", "")
        ):
            raise HTTPException(404, "画布不存在")
        return {
            "export": request.app.state.exports.submit(
                raw.get("requestId"), raw["documentId"], raw.get("timeline")
            )
        }

    @router.get("/api/exports/{ident}")
    async def get(ident: str, request: Request):
        job = request.app.state.exports.jobs.get(ident)
        if not job:
            raise HTTPException(404, "导出任务不存在")
        return {"export": job}

    @router.delete("/api/exports/{ident}")
    async def cancel(ident: str, request: Request):
        return {"export": await request.app.state.exports.cancel(ident)}

    @router.get("/api/exports/{ident}/download")
    async def download(ident: str, request: Request):
        exports = request.app.state.exports
        job = exports.jobs.get(ident)
        if not job or job["status"] != "completed":
            raise HTTPException(404, "成片尚未就绪")
        path = exports.root / ident / "output.mp4"
        if not path.is_file():
            raise HTTPException(404, "成片文件已丢失，请重新导出")
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=f"frayune-{ident}.mp4",
        )

    return router
