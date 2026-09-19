"""Versioned, frame-based edit decisions. Sources are snapshots, independent of canvas nodes."""

from copy import deepcopy
from math import isfinite


def validate_timeline(raw):
    if not isinstance(raw, dict) or raw.get("version") != 1 or raw.get("fps") != 30:
        raise ValueError("时间线版本或帧率不受支持")
    if "canvas" in raw:
        position = raw["canvas"]
        if not isinstance(position, dict) or any(
            type(position.get(key)) not in (int, float)
            or not isfinite(position[key])
            or abs(position[key]) > 1_000_000
            for key in ("x", "y")
        ):
            raise ValueError("时间线画布位置无效")
    for key in ("width", "height"):
        if type(raw.get(key)) is not int or not 64 <= raw[key] <= 4096:
            raise ValueError("时间线画幅无效")
    clips = raw.get("clips")
    if not isinstance(clips, list) or len(clips) > 2000:
        raise ValueError("时间线片段格式错误或超过 2000 项")
    ids = set()
    for clip in clips:
        if not isinstance(clip, dict):
            raise ValueError("时间线片段格式错误")
        ident = clip.get("id")
        if not isinstance(ident, str) or not ident or len(ident) > 100 or ident in ids:
            raise ValueError("时间线片段标识无效或重复")
        ids.add(ident)
        if not isinstance(clip.get("name"), str) or len(clip["name"]) > 500:
            raise ValueError("时间线片段名称无效")
        if "subtitle" in clip and (
            not isinstance(clip["subtitle"], str) or len(clip["subtitle"]) > 4000
        ):
            raise ValueError("片段字幕无效或超过 4000 字")
        volume = clip.get("volume", 1)
        if type(volume) not in (int, float) or not 0 <= volume <= 1:
            raise ValueError("片段音量必须在 0 到 1 之间")
        for fade in ("fadeInFrames", "fadeOutFrames"):
            if type(clip.get(fade, 0)) is not int or not 0 <= clip.get(fade, 0) <= 108000:
                raise ValueError("音频淡入淡出时长无效")
        src = clip.get("source")
        if not isinstance(src, dict) or src.get("kind") not in ("image", "video"):
            raise ValueError("时间线素材类型无效")
        if not isinstance(src.get("nodeId"), str):
            raise ValueError("时间线来源节点无效")
        url = src.get("url")
        if (
            not isinstance(url, str)
            or not url
            or not (
                (url.startswith("/") and not url.startswith("//"))
                or url.startswith(("https://", "http://", "data:image/"))
            )
        ):
            raise ValueError("时间线素材地址无效")
        values = [clip.get("inFrame"), clip.get("outFrame"), src.get("durationFrames")]
        if (
            any(type(v) is not int for v in values)
            or not 0 <= values[0] < values[1] <= values[2] <= 108000
        ):
            raise ValueError("时间线入出点超出素材时长")
    validate_audio_clips(raw.get("audioClips", []))
    return deepcopy(raw)


def timeline_srt(timeline):
    def stamp(frame):
        milliseconds = (frame * 1000 + 15) // 30
        seconds, ms = divmod(milliseconds, 1000)
        minutes, sec = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        return f"{hours:02}:{minute:02}:{sec:02},{ms:03}"

    import re

    frame = 0
    cues = []
    for clip in timeline["clips"]:
        end = frame + clip["outFrame"] - clip["inFrame"]
        text = re.sub(
            r"\n\s*\n", "\n", clip.get("subtitle", "").replace("\r\n", "\n").replace("\r", "\n")
        ).strip()
        if text:
            cues.append(f"{len(cues) + 1}\n{stamp(frame)} --> {stamp(end)}\n{text}")
        frame = end
    return "\n\n".join(cues) + "\n" if cues else ""


def validate_audio_clips(clips):
    import re

    if not isinstance(clips, list) or len(clips) > 64:
        raise ValueError("音频片段不能超过 64 项")
    ids = set()
    for clip in clips:
        if not isinstance(clip, dict):
            raise ValueError("音频片段格式无效")
        ident = clip.get("id")
        if not isinstance(ident, str) or not ident or len(ident) > 100 or ident in ids:
            raise ValueError("音频片段标识无效或重复")
        ids.add(ident)
        if not isinstance(clip.get("name"), str) or len(clip["name"]) > 500:
            raise ValueError("音频名称无效")
        source = clip.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("assetId"), str):
            raise ValueError("音频资产无效")
        asset = source["assetId"]
        if (
            not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", asset)
            or source.get("url") != f"/assets/{asset}/original.wav"
        ):
            raise ValueError("音频来源必须是资产库 WAV")
        start, inside, outside, duration = (
            clip.get("startFrame"),
            clip.get("inFrame"),
            clip.get("outFrame"),
            source.get("durationFrames"),
        )
        if (
            any(type(v) is not int for v in (start, inside, outside, duration))
            or not 0 <= inside < outside <= duration <= 108000
            or start < 0
            or start + outside - inside > 108000
        ):
            raise ValueError("音频起始位置或裁剪范围无效")
        volume = clip.get("volume")
        if type(volume) not in (int, float) or not 0 <= volume <= 1:
            raise ValueError("音频音量无效")
        for field in ("fadeInFrames", "fadeOutFrames"):
            if type(clip.get(field)) is not int or not 0 <= clip[field] <= 108000:
                raise ValueError("音频淡化时长无效")
