"""Hard artifact checks operate outside database transactions; content review is separate."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
import shutil

from PIL import Image, ImageOps

from .catalog import HostError


def managed_path(root: Path, asset) -> Path:
    path = (root / asset.storage_key).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise HostError("artifact_missing", "生成文件不可用")
    if path.stat().st_size != asset.bytes or asset.bytes <= 0 or asset.bytes > 512 * 1024**2:
        raise HostError("artifact_invalid", "生成文件大小无效")
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != asset.sha256:
        raise HostError("artifact_invalid", "生成文件完整性检查未通过")
    return path


async def bounded_command(args, timeout=30):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode:
            raise HostError("artifact_invalid", "媒体文件无法解码")
        return out
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.communicate()
        raise


async def inspect(asset, spec, root: Path):
    try:
        path = await asyncio.to_thread(managed_path, root, asset)
        if asset.kind == 'image':
            def image_check():
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    if (image.width, image.height) != (spec.width, spec.height):
                        raise HostError('spec_not_allowed', '生成图片尺寸与能力规格不符')
                    return {'width': image.width, 'height': image.height, 'bytes': asset.bytes}
            metadata = await asyncio.to_thread(image_check)
        elif asset.kind == 'video':
            probe = shutil.which('ffprobe')
            ffmpeg = shutil.which('ffmpeg')
            if not probe or not ffmpeg:
                raise HostError('media_probe_unavailable', '视频质检运行时未安装')
            raw = await bounded_command([probe, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                'stream=codec_name,width,height,nb_frames:format=duration', '-of', 'json', str(path)])
            measured = json.loads(raw)
            streams = measured.get('streams') or []
            duration = float(measured.get('format', {}).get('duration', 0))
            if not streams or streams[0].get('width') != spec.width or streams[0].get('height') != spec.height:
                raise HostError('spec_not_allowed', '生成视频尺寸与能力规格不符')
            if abs(duration - spec.durationSec) > max(0.25, spec.durationSec * 0.1):
                raise HostError('spec_not_allowed', '生成视频时长与能力规格不符')
            # Decode multiple sampled frames. A static frame encoded as MP4 cannot pass as motion.
            fps = 3 / max(1, duration)
            decoded = await bounded_command([ffmpeg, '-v', 'error', '-nostdin', '-i', str(path), '-vf',
                f'fps={fps},scale=64:64', '-frames:v', '4', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
            frame_size = 64 * 64 * 3
            frames = [decoded[index:index + frame_size] for index in range(0, len(decoded), frame_size)
                      if len(decoded[index:index + frame_size]) == frame_size]
            differences = [sum(abs(left - right) for left, right in zip(frames[0], frame)) / frame_size
                           for frame in frames[1:]]
            if len(frames) < 2 or max(differences, default=0) <= 0.5:
                raise HostError('video_static_output', '视频质检检测到静止画面，未作为视频完成')
            metadata = {'width': spec.width, 'height': spec.height, 'durationSec': duration,
                        'sampleCount': len(frames), 'motionDetected': True, 'motionDifference': max(differences), 'bytes': asset.bytes}
        else:
            raise HostError('artifact_type_mismatch', '生成产物类型无效')
        return {'passed': True, 'checks': metadata, 'sha256': asset.sha256}
    except (HostError, OSError, ValueError, TimeoutError, Image.DecompressionBombError) as exc:
        return {'passed': False, 'code': exc.code if isinstance(exc, HostError) else 'artifact_invalid',
                'message': exc.message if isinstance(exc, HostError) else '生成文件质检未通过', 'sha256': asset.sha256}


async def vision_samples(asset, root: Path) -> list[str]:
    """Only managed, bounded JPEG samples enter the vision request, never remote asset URLs."""
    import base64
    path = await asyncio.to_thread(managed_path, root, asset)
    def encode_image(source):
        with Image.open(source) as image:
            image = ImageOps.contain(image.convert('RGB'), (512, 512))
            buffer = io.BytesIO()
            image.save(buffer, 'JPEG', quality=80)
        return 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode()
    if asset.kind == 'image':
        return [await asyncio.to_thread(encode_image, path)]
    binary = shutil.which('ffmpeg')
    if not binary:
        raise HostError('media_probe_unavailable', '视频抽帧运行时未安装')
    out = await bounded_command([binary, '-v', 'error', '-nostdin', '-i', str(path), '-vf',
        'fps=1,scale=512:512:force_original_aspect_ratio=decrease', '-frames:v', '3', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'])
    # JPEG SOI/EOI delimiters are safe here: the stream is generated by our encoder.
    result = []
    for part in out.split(b'\xff\xd8')[1:]:
        end = part.find(b'\xff\xd9')
        if end >= 0:
            result.append('data:image/jpeg;base64,' + base64.b64encode(b'\xff\xd8' + part[:end + 2]).decode())
    if not result:
        raise HostError('artifact_invalid', '视频抽帧失败')
    return result
