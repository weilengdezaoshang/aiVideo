import asyncio
import subprocess
from uuid import uuid4

import pytest
from PIL import Image

from backend.exports import Exports, ffmpeg_binary, source_path


def make_timeline(tmp_path):
    clips = []
    for color, frames in [("red", 30), ("blue", 60)]:
        ident = str(uuid4())
        directory = tmp_path / "assets" / ident
        directory.mkdir(parents=True)
        Image.new("RGB", (160, 90), color).save(directory / "original.png")
        clips.append(
            dict(
                id=str(uuid4()),
                name=color,
                source=dict(
                    nodeId=ident,
                    kind="image",
                    url=f"/assets/{ident}/original.png",
                    durationFrames=1000,
                ),
                inFrame=15,
                outFrame=15 + frames,
            )
        )
    return dict(version=1, fps=30, width=160, height=90, clips=clips)


def test_real_render_exact_frames_order_audio_and_recovery(tmp_path):
    async def run():
        exports = Exports(tmp_path)
        ident = str(uuid4())
        timeline = make_timeline(tmp_path)
        job = exports.submit(ident, "doc", timeline)
        assert exports.submit(ident, "doc", timeline) is job
        timeline["clips"][0]["outFrame"] = 999
        await exports.tasks[ident]
        assert job["status"] == "completed", job.get("error")
        assert Exports(tmp_path).jobs[ident]["status"] == "completed"
        return exports.root / ident / "output.mp4"

    output = asyncio.run(run())
    # Decode the actual output, rather than trusting job status or command construction.
    raw = subprocess.run(
        [
            ffmpeg_binary(),
            "-v",
            "error",
            "-i",
            str(output),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    size = 160 * 90 * 3
    assert len(raw) // size == 90
    assert raw[10 * size] > 240 and raw[10 * size + 2] < 10
    assert raw[70 * size] < 10 and raw[70 * size + 2] > 240
    assert raw[29 * size] > 240
    assert raw[30 * size + 2] > 240
    audio = subprocess.run(
        [ffmpeg_binary(), "-v", "error", "-i", str(output), "-map", "0:a:0", "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    ).stdout
    assert len(audio) >= 3 * 48000 * 2 * 2


def test_reject_remote_and_traversal(tmp_path):
    for url in [
        "http://localhost/secret",
        "/assets/../../secret",
        "file:///etc/passwd",
        "/images/../secret",
        "/brand/logo.svg",
    ]:
        with pytest.raises(ValueError):
            source_path(tmp_path, url)


def test_immediate_cancel_and_restart_do_not_render_again(tmp_path):
    async def run():
        exports = Exports(tmp_path)
        ident = str(uuid4())
        exports.submit(ident, "doc", make_timeline(tmp_path))
        job = await exports.cancel(ident)
        assert job["status"] == "cancelled"
        assert not exports.tasks
        assert not (exports.root / ident / "output.mp4").exists()
        job.update(status="running")
        exports.save(job)
        recovered = Exports(tmp_path)
        assert recovered.jobs[ident]["status"] == "failed"
        assert not recovered.tasks

    asyncio.run(run())


@pytest.mark.parametrize("volume,fade", [(1, 0), (0, 0), (0.5, 15)])
def test_real_video_trim_keeps_source_frames_and_audio(tmp_path, volume, fade):
    binary = ffmpeg_binary()
    ident = str(uuid4())
    directory = tmp_path / "assets" / ident
    directory.mkdir(parents=True)
    subprocess.run(
        [
            binary,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=48000",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(directory / "original.mp4"),
        ],
        check=True,
    )

    async def run():
        exports = Exports(tmp_path)
        job_id = str(uuid4())
        clip = dict(
            id="video",
            name="测试",
            volume=volume,
            fadeInFrames=fade,
            fadeOutFrames=fade,
            source=dict(
                nodeId=ident, kind="video", url=f"/assets/{ident}/original.mp4", durationFrames=90
            ),
            inFrame=30,
            outFrame=60,
        )
        job = exports.submit(
            job_id, "doc", dict(version=1, fps=30, width=160, height=90, clips=[clip])
        )
        await exports.tasks[job_id]
        assert job["status"] == "completed", job.get("error")
        return exports.root / job_id / "output.mp4"

    output = asyncio.run(run())
    raw = subprocess.run(
        [binary, "-v", "error", "-i", str(output), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
        check=True,
    ).stdout
    assert len(raw) == 30 * 160 * 90 * 3
    expected = subprocess.run(
        [
            binary,
            "-v",
            "error",
            "-ss",
            "1",
            "-i",
            str(directory / "original.mp4"),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    assert sum(abs(a - b) for a, b in zip(raw[: len(expected)], expected)) / len(expected) < 8
    audio = subprocess.run(
        [binary, "-v", "error", "-i", str(output), "-map", "0:a:0", "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    ).stdout
    if volume == 0:
        assert not any(audio)
    else:
        assert any(audio)
    if fade:
        from array import array

        samples = array("h", audio)

        def energy(start, end):
            values = samples[int(start * 48000) * 2 : int(end * 48000) * 2]
            return sum(value * value for value in values) / len(values)

        assert energy(0.02, 0.1) < energy(0.4, 0.5) * 0.2
        assert energy(0.9, 0.98) < energy(0.4, 0.5) * 0.2


def test_export_api_accepts_idempotently_and_downloads_real_file(client, png):
    import time

    asset = client.post("/api/assets?ext=png&kind=image", content=png).json()["asset"]
    doc = client.post("/api/documents", json={"name": "导出测试"}).json()["document"]
    ident = str(uuid4())
    clip = dict(
        id="clip",
        name="图",
        source=dict(
            nodeId="n", kind="image", url=f"/assets/{asset['id']}/original.png", durationFrames=30
        ),
        inFrame=0,
        outFrame=30,
    )
    payload = dict(
        documentId=doc["id"],
        requestId=ident,
        timeline=dict(version=1, fps=30, width=160, height=90, clips=[clip]),
    )
    assert client.post("/api/exports", json=payload).status_code == 202
    assert client.post("/api/exports", json=payload).json()["export"]["id"] == ident
    end = time.monotonic() + 10
    while time.monotonic() < end:
        job = client.get(f"/api/exports/{ident}").json()["export"]
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(0.03)
    assert job["status"] == "completed", job
    result = client.get(f"/api/exports/{ident}/download")
    assert result.status_code == 200
    assert result.headers["content-type"] == "video/mp4"
    assert b"ftyp" in result.content[:32]
    assert client.delete(f"/api/exports/{ident}").json()["export"]["status"] == "completed"
    payload["timeline"]["width"] = 180
    assert client.post("/api/exports", json=payload).status_code == 409


def test_cancel_running_render_cleans_partial_files(tmp_path):
    async def run():
        exports = Exports(tmp_path)
        timeline = make_timeline(tmp_path)
        timeline["width"], timeline["height"] = 1920, 1080
        timeline["clips"] = timeline["clips"][:1]
        timeline["clips"][0]["source"]["durationFrames"] = 108000
        timeline["clips"][0]["outFrame"] = 108000
        ident = str(uuid4())
        job = exports.submit(ident, "doc", timeline)
        await asyncio.sleep(0.15)
        assert job["status"] == "running"
        await exports.cancel(ident)
        assert job["status"] == "cancelled"
        assert not exports.tasks
        assert not (exports.root / ident / "work").exists()
        assert not (exports.root / ident / "output.mp4").exists()

    asyncio.run(run())


def test_export_contains_real_selectable_subtitle_track(tmp_path):
    async def run():
        exports = Exports(tmp_path)
        timeline = make_timeline(tmp_path)
        timeline["clips"][1]["subtitle"] = "产品登场\n欢迎体验"
        ident = str(uuid4())
        job = exports.submit(ident, "doc", timeline)
        await exports.tasks[ident]
        assert job["status"] == "completed", job.get("error")
        return exports.root / ident / "output.mp4"

    output = asyncio.run(run())
    extracted = subprocess.run(
        [ffmpeg_binary(), "-v", "error", "-i", str(output), "-map", "0:s:0", "-f", "srt", "-"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    assert "00:00:01,000 --> 00:00:03,000" in extracted
    assert "产品登场\n欢迎体验" in extracted


def test_mix_external_audio_at_exact_timeline_offset(tmp_path):
    import math
    import wave
    from array import array

    asset = str(uuid4())
    directory = tmp_path / "assets" / asset
    directory.mkdir(parents=True)
    samples = array(
        "h", (int(8000 * math.sin(2 * math.pi * 440 * n / 48000)) for n in range(96000))
    )
    with wave.open(str(directory / "original.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(samples.tobytes())

    async def run():
        exports = Exports(tmp_path)
        timeline = make_timeline(tmp_path)
        timeline["clips"][1]["subtitle"] = "音乐开始"
        timeline["audioClips"] = [
            dict(
                id="music",
                name="音乐",
                source=dict(assetId=asset, url=f"/assets/{asset}/original.wav", durationFrames=60),
                startFrame=30,
                inFrame=15,
                outFrame=45,
                volume=0.5,
                fadeInFrames=0,
                fadeOutFrames=0,
            )
        ]
        ident = str(uuid4())
        job = exports.submit(ident, "doc", timeline)
        await exports.tasks[ident]
        assert job["status"] == "completed", job.get("error")
        return exports.root / ident / "output.mp4"

    output = asyncio.run(run())
    raw = subprocess.run(
        [
            ffmpeg_binary(),
            "-v",
            "error",
            "-i",
            str(output),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    decoded = array("h", raw)

    def energy(start, end):
        values = decoded[int(start * 48000) : int(end * 48000)]
        return sum(v * v for v in values) / len(values)

    assert energy(1.2, 1.8) > 100000
    assert energy(0.2, 0.8) < 10
    assert energy(2.2, 2.8) < 10
    subtitles = subprocess.run(
        [ffmpeg_binary(), "-v", "error", "-i", str(output), "-map", "0:s:0", "-f", "srt", "-"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    assert "音乐开始" in subtitles
