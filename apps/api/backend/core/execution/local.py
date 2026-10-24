"""Reuse local media composition and export implementations, no provider generation."""
import asyncio
import uuid
import json
from dataclasses import asdict, replace
import hashlib
import io
from PIL import Image
from fastapi import HTTPException
from .masks import save_mask, selected_mask
from backend.infrastructure.artifacts import ArtifactSpool
from backend.providers.base import Generated
from types import SimpleNamespace

from backend.agent.media.frame_composition import render_frames
from backend.exports import Exports, ffmpeg_binary
from backend.storage import apply_mask
from .contracts import ProviderResult, WorkerSettings


_recognition = None


def recognition_service(timeout):
    global _recognition
    if _recognition is None:
        import os
        from backend.cutout import Recognition
        _recognition = Recognition(os.environ.get("CUTOUT_MODEL", "u2net"), timeout=timeout)
    return _recognition


class LocalAdapter:
    idempotent = True
    query_registration = True
    cancellable = False  # In-flight local computation cannot be declared cancelled by a remote ACK.

    def __init__(self, tool, factory, root):
        self.tool, self.root = tool, root
        self.settings = WorkerSettings()

    def _locked_composition(self):
        from backend.core.composition.errors import CompositionError
        from backend.core.composition.fonts import load_registry, locked_faces
        from backend.core.tools.contracts import ToolSettings
        snapshot = (self.tool.execution.binding or {}).get("composition")
        if not isinstance(snapshot, dict) or "fonts" not in snapshot:
            raise CompositionError("param_invalid")
        # The live registry only locates files. Identity is the published hash.
        return snapshot, locked_faces(snapshot, load_registry(ToolSettings().font_registry_path, int(snapshot["max_font_bytes"])))

    @staticmethod
    def _preset_table(snapshot):
        return {key: (int(value["width"]), int(value["height"])) for key, value in snapshot["presets"].items()}

    @staticmethod
    def _region_table(snapshot):
        return {key: (float(value["x"]), float(value["y"]), float(value["width"]), float(value["height"]))
                for key, value in snapshot["regions"].items()}

    async def submit(self, registration_id, params, idempotency_key):
        try:
            result = await self.compute(registration_id, params)
            epoch = params["_execution_epoch"]
            if result.data:
                artifact = ArtifactSpool(self.root).save(uuid.UUID(registration_id), epoch, Generated(result.data, result.ext))
                locator = {"local_file": artifact["storage_key"]}
                if result.output is not None:
                    locator["composition"] = result.output
                result = replace(result, data=None, output=None, locator=locator)
            marker = self.root / "export-work" / registration_id / str(epoch) / "execution-ready.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            ArtifactSpool._atomic(marker, json.dumps(asdict(result)).encode())
            return result
        except HTTPException as exc:
            from backend.core.tools.errors import normalize_legacy_cutout
            failure = normalize_legacy_cutout(exc.status_code)
            return ProviderResult("failed", registration_id, failure=failure.kind)
        except (ValueError, KeyError):
            return ProviderResult("failed", registration_id, failure="param_invalid")

    async def compute(self, registration_id, params):
        adapter = self.tool.execution.adapter
        def source(item):
            path = (self.root / item["storage_key"]).resolve()
            if not path.is_relative_to(self.root.resolve()):
                raise ValueError("invalid MediaHost source")
            return path
        if adapter == "recognition.detect":
            from backend.core.tools.detection import ImageIdentity, normalize_rembg
            source_data = source(params["image"]).read_bytes()
            with Image.open(io.BytesIO(source_data)) as image:
                identity = ImageIdentity(asset_id=params["image"]["asset_id"],
                    sha256=hashlib.sha256(source_data).hexdigest(), width=image.width, height=image.height)
            recognition = recognition_service(self.tool.execution.sla_seconds)
            try:
                mask = await recognition.detect(registration_id, source_data)
                handle = registration_id + ":foreground"
                result = normalize_rembg(mask, identity, registration_id, handle)
                if result.candidate_count:
                    save_mask(self.root, handle, mask)
                return ProviderResult("succeeded", registration_id, output=result.model_dump(mode="json"))
            finally:
                recognition.finish(registration_id)

        if adapter in {"cards.create", "cards.update"}:
            # The card write happens in the worker's consume transaction; the adapter
            # only marks the attempt finished so submission stays durable and replay-safe.
            return ProviderResult("succeeded", registration_id, output={"applied": False})

        if adapter == "composition.text_layout":
            from backend.core.composition.errors import CompositionError
            from backend.core.composition.layout import layout_text
            try:
                snapshot, registry = self._locked_composition()
                if snapshot.get("text_overflow") != "fail":
                    raise CompositionError("param_invalid")
                laid = layout_text({key: value for key, value in params.items() if not key.startswith("_")},
                    registry, min_font_size=snapshot["min_font_size"], max_font_size=snapshot["max_font_size"],
                    max_copy_chars=snapshot["max_copy_chars"], min_edge=snapshot["min_canvas_edge"],
                    max_edge=snapshot["max_canvas_edge"], line_height=snapshot["text_line_height"],
                    max_lines=snapshot["max_text_lines"], presets=self._preset_table(snapshot),
                    regions=self._region_table(snapshot))
            except CompositionError:
                return ProviderResult("failed", registration_id, failure="param_invalid")
            return ProviderResult("succeeded", registration_id, output=laid, actual_credits=self.tool.price.credits or 0)
        if adapter == "composition.compose":
            from backend.core.composition.compose import compose_layers
            from backend.core.composition.errors import CompositionError
            clean = {key: value for key, value in params.items() if not key.startswith("_")}
            try:
                snapshot, registry = self._locked_composition()
                subject = clean.get("subject")
                subject_bytes = source(subject).read_bytes() if subject else None
                background = clean["background"]
                background_bytes = None
                background_sha = background_id = None
                if background.get("kind") == "image":
                    image = background["image"]
                    background_bytes = source(image).read_bytes()
                    background_sha = image["sha256"]
                    if hashlib.sha256(background_bytes).hexdigest() != background_sha:
                        raise CompositionError("param_invalid")
                    background_id = image.get("asset_id")
                texts = []
                for item in clean.get("text_layers") or []:
                    blob = source(item).read_bytes()
                    if item.get("sha256") and hashlib.sha256(blob).hexdigest() != item["sha256"]:
                        raise CompositionError("param_invalid")
                    payload = json.loads(blob)
                    payload["asset_id"] = item.get("asset_id")
                    payload["sha256"] = item.get("sha256")
                    payload["core_asset_version_id"] = item.get("core_asset_version_id")
                    texts.append(payload)
                proof = clean.get("subject_proof")
                mask_bytes = None
                if proof and proof.get("mask"):
                    mask_bytes = selected_mask(self.root, proof["mask"])
                data, manifest = compose_layers(width=clean.get("width"), height=clean.get("height"),
                    preset=clean.get("preset"), background=background, texts=texts,
                    registry=registry, min_edge=snapshot["min_canvas_edge"],
                    max_edge=snapshot["max_canvas_edge"], subject_margin_ratio=snapshot["subject_margin_ratio"],
                    max_pixels=snapshot["max_image_pixels"], presets=self._preset_table(snapshot),
                    subject_png=subject_bytes, subject_asset_id=None if subject is None else subject.get("asset_id"),
                    subject_sha256=None if subject is None else subject.get("sha256"),
                    subject_proof=proof, mask_png=mask_bytes,
                    background_asset_id=background_id, background_sha256=background_sha, background_png=background_bytes)
            except CompositionError as exc:
                return ProviderResult("failed", registration_id, failure="no_subject" if exc.code == "need_subject" else "param_invalid")
            if self.tool.price.credits is None:
                return ProviderResult("failed", registration_id, failure="param_invalid")
            return ProviderResult("succeeded", registration_id, data=data, output=manifest,
                                  actual_credits=self.tool.price.credits, ext="png")
        if adapter == "storage.apply_mask":
            # The host must resolve an explicitly approved mask asset, never a model path.
            source_data = source(params["image"]).read_bytes()
            from backend.core.tools.detection import MaskReference, ImageIdentity, validate_mask_source
            ref = params["mask"]
            with Image.open(io.BytesIO(source_data)) as image:
                identity = ImageIdentity(asset_id=params["image"]["asset_id"], sha256=hashlib.sha256(source_data).hexdigest(),
                    width=image.width, height=image.height)
            validate_mask_source(MaskReference.model_validate({k: v for k, v in ref.items() if not k.startswith("_")}), identity)
            data = await asyncio.to_thread(apply_mask, source_data, selected_mask(self.root, ref))
            return ProviderResult("succeeded", registration_id, data=data,
                                  actual_credits=self.tool.price.credits)
        if adapter == "frames_to_video":
            binding = self.tool.execution.binding
            spec = binding["spec"]
            claim = SimpleNamespace(job_id=uuid.UUID(registration_id), epoch=params["_execution_epoch"], params={
                "frameSources": [{"key": item["storage_key"]} for item in params["frames"]],
                "width": spec["width"], "height": spec["height"], "fps": spec["fps"], "durationSec": spec["durationSec"]})
            output = await render_frames(self.root, claim, ffmpeg_binary())
            return ProviderResult("succeeded", registration_id, locator={"local_file": str(output.relative_to(self.root))},
                                  actual_credits=spec["credits"], ext="mp4")
        if adapter == "export.timeline":
            # Dimensions and durations are measured by the host, not supplied as canvas coordinates.
            clips, paths = [], {}
            from backend.timeline import validate_timeline
            for index, item in enumerate(params["clips"]):
                url = "asset:" + item["asset_id"]
                paths[url] = source(item)
                duration = item.get("duration_frames")
                if not duration:
                    duration = await self.duration_frames(source(item))
                clips.append({"id": str(index), "name": "clip", "source": {"kind": "video", "nodeId": "", "url": "/" + url,
                    "durationFrames": duration}, "inFrame": 0, "outFrame": duration})
                paths["/" + url] = paths[url]
            timeline = validate_timeline({"version": 1, "fps": 30, "width": self.settings.export_width, "height": self.settings.export_height, "clips": clips})
            renderer = Exports(self.root, durable=True, source_resolver=lambda url: paths[url],
                               work_root=self.root / "export-work" / registration_id / str(params["_execution_epoch"]))
            job = {"id": registration_id, "status": "queued", "timeline": timeline}
            await renderer.render(job, ffmpeg_binary())
            output = renderer.root / registration_id / "output.mp4"
            return ProviderResult("succeeded", registration_id, locator={"local_file": str(output.relative_to(self.root))},
                                  actual_credits=self.tool.price.credits, ext="mp4")
        raise ValueError("unsupported local adapter")

    async def duration_frames(self, source):
        """Reuse the installed FFmpeg runtime; inspect only an owned local file."""
        import re
        process = await asyncio.create_subprocess_exec(ffmpeg_binary(), "-hide_banner", "-i", str(source),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            data = await process.stderr.read(self.settings.probe_max_bytes + 1)
            if len(data) > self.settings.probe_max_bytes:
                raise ValueError("video metadata exceeds limit")
            await process.wait()
            match = re.search(rb"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", data)
            if not match:
                raise ValueError("video duration unavailable")
            seconds = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
            return round(seconds * 30)  # Existing timeline protocol is fixed at 30 fps.
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def query(self, task_id, registration_id):
        for marker in (self.root / "export-work" / registration_id).glob("*/execution-ready.json"):
            result = ProviderResult(**json.loads(marker.read_text()))
            if result.output is not None:
                return result
            path = (self.root / result.locator["local_file"]).resolve()
            if path.is_relative_to(self.root.resolve()) and path.is_file():
                return result
        return None

    async def download(self, locator, kind):
        path = (self.root / locator["local_file"]).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("invalid local artifact")
        return await asyncio.to_thread(path.read_bytes), path.suffix.lstrip(".")

    async def cancel(self, task_id):
        raise ValueError("local native computation cannot be remotely cancelled")
