"""Import existing bindings and read schemas; no provider preparation or execution.

Seed properties belong only to a newly built catalog. An already published snapshot
is immutable and is not rewritten. Publish the new content under a new version string:

  PYTHONPATH=apps/api python scripts/tool_catalog.py --host-catalog <catalog.json> \\
    --version <new-version> --publish --published-by <name>

That command requires an explicit AIVERO_DB_URL. Reusing a published version conflicts.
Runs locked to the old version stay executable when seed is absent.
"""
from __future__ import annotations

from backend.agent.media.catalog import Catalog
from backend.agent.tools.canvas_read import READ_TOOLS
from backend.config import Config
from backend.providers.seed_capability import provider_honors_seed
from backend.providers.staged import StagedProvider

from .catalog import ToolCatalog
from .contracts import CatalogSnapshot, Execution, Port, Price, ToolDefinition, ToolSettings
from .errors import FAILURE_RULES


def object_schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": False}


def reference_schema(media_type: str) -> dict:
    # Model emits references, never media bytes, URLs, coordinates, or worker credentials.
    literal = object_schema({"asset_id": {"type": "string", "minLength": 1},
                             "media_type": {"const": media_type}})
    if media_type == "mask":
        literal = object_schema({"handle": {"type": "string", "minLength": 1},
            "media_type": {"const": "mask"}, "source_asset_id": {"type": "string", "minLength": 1},
            "source_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1},
            "operation_id": {"type": "string", "minLength": 1}})
    return {"anyOf": [literal, {"type": "string", "pattern": r"^\$[a-z][a-z0-9_]*\.[a-zA-Z0-9_.\[\]]+$"}]}


DETECT_RESULT = object_schema({
    "result_type": {"enum": ["detected", "no_subject"]},
    "candidate_count": {"type": "integer", "minimum": 0},
    "granularity": {"enum": ["combined_foreground", "instances"]},
    "candidates": {"type": "array", "items": object_schema({
        "mask": reference_schema("mask")["anyOf"][0],
        "label": {"type": "string", "minLength": 1},
        "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "confidence_source": {"enum": ["provider", "unknown"]}})},
})
# Discriminated outcomes: an empty array is a successful no_subject result, never a failed call.
DETECT_RESULT["oneOf"] = [
    {"properties": {"result_type": {"const": "detected"}, "candidate_count": {"minimum": 1},
                    "candidates": {"minItems": 1}}},
    {"properties": {"result_type": {"const": "no_subject"}, "candidate_count": {"const": 0},
                    "candidates": {"maxItems": 0}}},
]


def build_catalog(legacy: Catalog, version: str, settings: ToolSettings | None = None) -> ToolCatalog:
    settings = settings or ToolSettings()
    tools = []
    def add(name, description, params, inputs, outputs, mode, price, execution, *, limits=None,
            result=None, available=True, reason=None, tool_version="1"):
        tools.append(ToolDefinition(name=name, version=tool_version, description=description,
            parameters=params, result_schema=result or object_schema({"output": reference_schema(outputs[0].media_type)["anyOf"][0]}),
            inputs=inputs, outputs=outputs, mode=mode, price=price, execution=execution,
            limits=limits or {}, failures=FAILURE_RULES, available=available, unavailable_reason=reason,
            rollout_groups=settings.rollout_groups.get(name, [])))

    for spec in READ_TOOLS:
        add(spec.name, spec.description + " 需要创作时应写生成任务单。", spec.input.model_json_schema(by_alias=True),
            [], [Port(path="data", media_type="text")], "read_only", Price(kind="fixed", credits=0),
            Execution(adapter="canvas_read:" + spec.name, result_url_policy="authenticated_asset",
                      sla_seconds=spec.timeout_s), result={"type": "object"}, tool_version=str(spec.version))

    add("read_asset", "读取授权范围内资产的基本元数据；测量图片原文件应使用 inspect_image，创作应写生成任务单。",
        object_schema({"asset_id": {"type": "string", "minLength": 1}}),
        [], [Port(path="data", media_type="text")], "read_only", Price(kind="fixed", credits=0),
        Execution(adapter="asset.metadata", result_url_policy="authenticated_asset",
            sla_seconds=settings.local_timeout_seconds), result={"type": "object"})

    local = dict(result_url_policy="authenticated_asset", sla_seconds=settings.local_timeout_seconds)
    add("detect_subject", "识别图片前景候选并返回临时蒙版；rembg 仅返回合并前景，置信度未知。需要合成时使用 cutout。",
        object_schema({"image": reference_schema("image"), "target_description": {"type": "string"}}, required=["image"]), [Port(path="image", media_type="image")],
        [Port(path="candidates[*].mask", media_type="mask", when={"result_type": "detected"}),
         Port(path="output", media_type="mask", when={"result_type": "detected"})],
        "read_only", Price(kind="fixed", credits=0),
        Execution(adapter="recognition.detect", cancellable=True, cancellation="cooperative",
            result_url_policy="transient", sla_seconds=settings.recognition_timeout_seconds),
        result={**DETECT_RESULT, "properties": {**DETECT_RESULT["properties"],
            "candidate_count": {"type": "integer", "minimum": 0, "maximum": settings.max_candidates},
            "candidates": {**DETECT_RESULT["properties"]["candidates"], "maxItems": settings.max_candidates}}}, limits={"max_candidates": settings.max_candidates,
            "max_mask_bytes": settings.max_mask_bytes, "max_image_pixels": settings.max_image_pixels,
            "confidence_nullable": True, "mask_persistence": "transient"}, tool_version="2")
    add("cutout", "按用户或任务单指定的一个蒙版合成新图片并保留原图；没有蒙版时先使用 detect_subject。",
        object_schema({"image": reference_schema("image"), "mask": reference_schema("mask")}),
        [Port(path="image", media_type="image"), Port(path="mask", media_type="mask")],
        [Port(path="output", media_type="image")], "deterministic",
        Price(kind="fixed", credits=settings.cutout_credits), Execution(adapter="storage.apply_mask", **local),
        limits={"same_source_required": True, "same_dimensions_required": True,
                "preserve_original": True, "max_mask_bytes": settings.max_mask_bytes,
                "max_image_pixels": settings.max_image_pixels})

    descriptions = {
        "text_to_image": "按文字生成图片；编辑已有图片改用 image_edit。",
        "image_edit": "编辑一张已有图片；只抠前景改用 detect_subject 和 cutout。",
        "text_to_video": "按明确的视频需求生成视频；视频需求不明确必须澄清，失败不得降级为图片。",
        "image_to_video": "将一张首帧图片生成视频；静态编辑改用 image_edit，视频失败不得降级为图片。",
        "frames_to_video": "将图片序列合成为视频；生成动态内容应使用视频生成工具。",
    }
    for cap in legacy.entries.values():
        for tier, spec in cap.tiers.items():
            name = f"{cap.id}_{tier}"
            params = {"prompt": {"type": "string", "minLength": 1},
                      "count": {"type": "integer", "minimum": 1, "maximum": settings.max_count},
                      "width": {"type": "integer", "enum": [spec.width]},
                      "height": {"type": "integer", "enum": [spec.height]}}
            inputs = []
            if cap.id in {"image_edit", "image_to_video"}:
                params["image"] = reference_schema("image")
                inputs.append(Port(path="image", media_type="image"))
            elif cap.id == "frames_to_video":
                params["frames"] = {"type": "array", "minItems": settings.min_frames, "maxItems": settings.max_count,
                                    "items": reference_schema("image")}
                inputs.append(Port(path="frames[*]", media_type="image"))
            if cap.outputType == "video":
                params["duration_seconds"] = {"type": "number", "enum": [spec.durationSec]}
                params["fps"] = {"type": "integer", "enum": [spec.fps]}
            binding = cap.binding
            snapshot = binding.providerSnapshot if binding else {}
            config = Config.model_validate(snapshot) if binding and binding.engine == "provider" else None
            capabilities = StagedProvider(config, None).capabilities(cap.outputType) if config else None
            seed_supported = bool(config and cap.id != "frames_to_video" and provider_honors_seed(
                config.provider, cap.outputType, config.cloudVendor))
            if seed_supported:
                params["seed"] = {"type": "integer", "minimum": settings.seed_minimum,
                                  "maximum": settings.seed_maximum}
            available = bool(legacy.config.enabled and cap.enabled and binding)
            execution = Execution(adapter="frames_to_video" if cap.id == "frames_to_video" else "staged",
                binding={"capability_id": cap.id, "tier": tier, "catalog_version": legacy.config.version,
                         "binding": binding.model_dump(mode="json") if binding else {},
                         "spec": spec.model_dump(mode="json"), "seed_supported": seed_supported},
                cancellable=bool(capabilities and capabilities.cancel) or cap.id == "frames_to_video",
                cancellation="provider" if capabilities and capabilities.cancel else "cooperative" if cap.id == "frames_to_video" else "none",
                provider_idempotent=bool(capabilities and capabilities.idempotent_submit),
                result_url_policy="authenticated_asset",
                sla_seconds=(config.videoTimeoutMin if cap.outputType == "video" else config.imageTimeoutMin) * 60
                    if config else settings.export_timeout_seconds)
            add(name, descriptions[cap.id], object_schema(params, [key for key in params if key != "seed"]), inputs,
                [Port(path="output", media_type=cap.outputType)],
                "deterministic" if cap.id == "frames_to_video" else "generate",
                Price(kind="per_count", credits=spec.credits, count_parameter="count"), execution,
                available=available, reason=None if available else "deployment_binding_disabled_or_missing",
                limits={"max_count": settings.max_count, "width": spec.width, "height": spec.height,
                        "duration_seconds": spec.durationSec, "fps": spec.fps, "max_ref_images": cap.maxRefImages},
                tool_version=binding.version if binding else "unbound")
            # Mask support exists only in the actual ComfyUI or SiliconFlow Fill request path.
            if cap.id == "image_edit":
                supported = bool(config and (config.provider == "comfyui" or (
                    config.provider == "cloud" and config.cloudVendor == "siliconflow"
                    and snapshot.get("cloudModel") == "black-forest-labs/FLUX.1-Fill-dev")))
                add(f"inpaint_{tier}", "按图片与蒙版局部重绘；不修改内容的前景抠图改用 cutout。",
                    object_schema({**params, "mask": reference_schema("mask")},
                                  [key for key in {**params, "mask": None} if key != "seed"]),
                    [*inputs, Port(path="mask", media_type="mask")], [Port(path="output", media_type="image")],
                    "generate", Price(kind="per_count", credits=spec.credits, count_parameter="count"),
                    execution.model_copy(update={"binding": {**execution.binding, "requires_mask": True}}),
                    available=available and supported,
                    reason=None if available and supported else "mask_binding_missing_or_would_replace_billed_model",
                    limits={"same_source_required": True, "same_dimensions_required": True},
                    tool_version=binding.version if binding else "unbound")

    # A deployment may omit capabilities entirely. Keep an honest inventory without
    # inventing a price or publishing unusable tools to the model.
    defined = {tool.name for tool in tools}
    for capability in (*descriptions, "inpaint"):
        for tier in ("preview", "final"):
            name = f"{capability}_{tier}"
            if name in defined:
                continue
            output_type = "video" if capability in {"text_to_video", "image_to_video", "frames_to_video"} else "image"
            params = {"prompt": {"type": "string", "minLength": 1}}
            inputs = []
            if capability in {"image_edit", "image_to_video", "inpaint"}:
                params["image"] = reference_schema("image")
                inputs.append(Port(path="image", media_type="image"))
            if capability == "inpaint":
                params["mask"] = reference_schema("mask")
                inputs.append(Port(path="mask", media_type="mask"))
            add(name, descriptions.get(capability, "局部重绘需要显式绑定支持蒙版的模型。"),
                object_schema(params), inputs, [Port(path="output", media_type=output_type)],
                "deterministic" if capability == "frames_to_video" else "generate",
                Price(kind="unpriced"), Execution(adapter="unavailable", **local),
                available=False, reason="deployment_binding_missing", tool_version="unbound")

    add("video_concat", "拼接已有视频时间线；图片序列使用 frames_to_video，生成视频使用视频生成工具。",
        object_schema({"clips": {"type": "array", "minItems": 1, "maxItems": settings.max_clips,
                                  "items": reference_schema("video")}}),
        [Port(path="clips[*]", media_type="video")], [Port(path="output", media_type="video")],
        "deterministic", Price(kind="fixed", credits=settings.export_credits) if settings.export_credits is not None
            else Price(kind="unpriced"),
        Execution(adapter="export.timeline", result_url_policy="authenticated_asset",
                  cancellable=True, cancellation="cooperative", sla_seconds=settings.export_timeout_seconds),
        limits={"max_clips": settings.max_clips, "max_frames": settings.max_frames},
        available=settings.export_credits is not None,
        reason=None if settings.export_credits is not None else "explicit_credit_price_required")
    for name, description in [("outpaint", "扩展图片边界；当前没有独立执行绑定。"),
                              ("upscale", "独立超分辨率放大；当前没有独立执行绑定，不能用重生成冒充。")]:
        add(name, description, object_schema({"image": reference_schema("image")}),
            [Port(path="image", media_type="image")], [Port(path="output", media_type="image")],
            "generate", Price(kind="unpriced"), Execution(adapter="unavailable", **local),
            available=False, reason="no_implemented_adapter")
    if set(settings.rollout_groups) - {tool.name for tool in tools}:
        raise ValueError("rollout configuration references unknown tools")
    return ToolCatalog(CatalogSnapshot(version=version, tools=tools))
