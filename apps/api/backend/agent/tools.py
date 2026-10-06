"""Read-only canvas tools: frozen projections in, measured observations out."""
from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path
from typing import Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError


ElementKind = Literal["image", "video", "placeholder", "error", "board", "text", "draft"]
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class ReadCanvasInput(ToolInput):
    pass


class ListElementsInput(ToolInput):
    kind: ElementKind | None = None
    offset: int = Field(default=0, ge=0, le=5000)
    limit: int = Field(default=50, ge=1, le=100)


class GetElementsInput(ToolInput):
    object_ids: list[str] = Field(alias="objectIds", min_length=1, max_length=50)


class InspectImageInput(ToolInput):
    asset_id: str = Field(alias="assetId", min_length=1, max_length=128)


class CanvasElement(BaseModel):
    """Allowlist excludes src URLs, generation inputs, masks and recognition state."""
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)
    id: str
    kind: ElementKind
    x: float
    y: float
    width: float
    height: float
    rotation: float | None = None
    name: str | None = None
    text: str | None = None
    fontSize: float | None = None
    fill: str | None = None
    backgroundColor: str | None = None
    directionTitle: str | None = None
    assetId: str | None = None
    ext: str | None = None
    mediaType: Literal["image", "video"] | None = None
    durationSec: float | None = None


class ImageObservation(BaseModel):
    assetId: str
    width: int
    height: int
    format: str
    bytes: int
    sha256: str


def project_snapshot(raw: dict, selected_ids: list[str]) -> dict:
    """Project exactly the saved revision; never persist transient canvas/UI state."""
    objects_raw = raw.get("objects")
    if not isinstance(objects_raw, dict) or len(objects_raw) > 5000:
        raise ToolError("INVALID_CANVAS", "画布对象格式无效")
    def visible_element(ident, value):
        visible = {**value, "id": ident}
        # Mirror the canvas's saved generation projection, without copying its
        # prompts, transient progress, masks or other execution state.
        generations = raw.get("generations") or {}
        generation_id = value.get("generationId")
        entity = generations.get(generation_id) if isinstance(generations, dict) and isinstance(generation_id, str) else None
        if isinstance(entity, dict):
            result = entity.get("result")
            if entity.get("status") == "completed" and isinstance(result, dict):
                visible.update({key: result.get(key) for key in ("kind", "assetId", "src", "ext")})
            elif entity.get("status") in {"failed", "cancelled"}:
                visible["kind"] = "error"
            else:
                visible["kind"] = "placeholder"
        # Existing generated media can carry only this internal content URL.
        # Extract an identity; this tool never fetches URLs or accepts external paths.
        if not visible.get("assetId") and isinstance(visible.get("src"), str):
            match = re.fullmatch(r"/api/assets/([0-9a-fA-F-]{36})/content", visible["src"])
            if match:
                visible["assetId"] = match[1]
        return visible

    try:
        objects = {ident: CanvasElement.model_validate(visible_element(ident, value)).model_dump(exclude_none=True)
                   for ident, value in objects_raw.items() if isinstance(ident, str) and isinstance(value, dict)}
    except ValidationError as error:
        raise ToolError("INVALID_CANVAS", "画布对象字段格式无效") from error
    if len(objects) != len(objects_raw):
        raise ToolError("INVALID_CANVAS", "画布对象格式无效")
    if any(ident not in objects for ident in selected_ids):
        raise ToolError("SELECTION_CHANGED", "发送时选中的对象已不存在")
    original_order = raw.get("order", [])
    if not isinstance(original_order, list) or any(not isinstance(ident, str) for ident in original_order):
        raise ToolError("INVALID_CANVAS", "画布顺序格式无效")
    order = list(dict.fromkeys(ident for ident in original_order if ident in objects))
    order.extend(ident for ident in objects if ident not in order)
    return {"id": str(raw.get("id", "")), "name": str(raw.get("name", "")),
            "revision": raw.get("revision"), "objects": objects, "order": order,
            "selectedObjectIds": list(dict.fromkeys(selected_ids))}


_TOOLS = {
    "read_canvas": (ReadCanvasInput, "读取本次运行冻结的画布版本、对象数量和发送时选中对象。"),
    "list_canvas_elements": (ListElementsInput, "按画布顺序分页列出对象摘要，可按类型筛选。"),
    "get_canvas_elements": (GetElementsInput, "获取指定对象的文字、布局属性和素材关联。"),
    "inspect_image": (InspectImageInput, "读取画布关联的真实图片文件，测量尺寸、格式、大小和哈希；不理解图片内容。"),
}


def tool_schemas() -> list[dict]:
    return [{"type": "function", "function": {"name": name, "description": description,
              "parameters": schema.model_json_schema(by_alias=True)}}
            for name, (schema, description) in _TOOLS.items()]


def _inspect_image(asset_id: str, snapshot: dict, assets: dict, media_root: Path) -> dict:
    if not any(element.get("assetId") == asset_id for element in snapshot["objects"].values()):
        raise ToolError("ASSET_OUT_OF_SCOPE", "素材不属于本次运行的画布快照")
    binding = assets.get(asset_id)
    if not isinstance(binding, dict):
        raise ToolError("ASSET_UNAVAILABLE", "素材尚未绑定到本次运行")
    key, expected_hash = binding.get("storage_key"), binding.get("sha256")
    expected_size = binding.get("bytes")
    if (not isinstance(key, str) or not key or Path(key).is_absolute()
            or not isinstance(expected_hash, str) or len(expected_hash) != 64
            or type(expected_size) is not int or expected_size < 1):
        raise ToolError("ASSET_UNAVAILABLE", "素材身份信息不完整")
    if expected_size > MAX_IMAGE_BYTES:
        raise ToolError("IMAGE_LIMIT", "图片超过 25 MiB 读取限制")
    root = Path(media_root).resolve()
    target = (root / key).resolve()
    if not target.is_relative_to(root) or ".." in Path(key).parts:
        raise ToolError("ASSET_OUT_OF_SCOPE", "素材路径超出共享媒体目录")
    try:
        with target.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ToolError("IMAGE_LIMIT", "图片超过 25 MiB 读取限制")
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != expected_size or digest != expected_hash:
            raise ToolError("ASSET_CHANGED", "图片文件与本次运行绑定的素材身份不符")
        with Image.open(io.BytesIO(data)) as picture:
            width, height, image_format = picture.width, picture.height, picture.format
            if width * height > MAX_IMAGE_PIXELS:
                raise ToolError("IMAGE_LIMIT", "图片超过 4000 万像素解码限制")
            picture.verify()
        with Image.open(io.BytesIO(data)) as picture:
            picture.load()
        return ImageObservation(assetId=asset_id, width=width, height=height,
            format=image_format or "unknown", bytes=len(data), sha256=digest).model_dump()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, SyntaxError, ValueError) as error:
        raise ToolError("IMAGE_UNREADABLE", "无法读取或解码绑定的图片文件") from error


def execute_tool(name: str, args: dict, snapshot: dict, assets: dict,
                 media_root: Path | str, max_output_bytes: int = 16384) -> dict:
    """Never query current document data; the Runtime owns these frozen inputs."""
    specification = _TOOLS.get(name)
    if specification is None:
        raise ToolError("UNKNOWN_TOOL", "工具不存在或不在只读工具列表中")
    try:
        parsed = specification[0].model_validate(args)
    except ValidationError as error:
        raise ToolError("INVALID_TOOL_ARGUMENTS", "工具参数无效") from error
    objects = snapshot["objects"]
    if isinstance(parsed, ReadCanvasInput):
        counts: dict[str, int] = {}
        for element in objects.values():
            counts[element["kind"]] = counts.get(element["kind"], 0) + 1
        result = {"documentId": snapshot["id"], "name": snapshot["name"],
                  "revision": snapshot["revision"], "objectCount": len(objects),
                  "countsByKind": counts, "selectedObjectIds": snapshot["selectedObjectIds"]}
    elif isinstance(parsed, ListElementsInput):
        ordered = [objects[ident] for ident in snapshot["order"]
                   if parsed.kind is None or objects[ident]["kind"] == parsed.kind]
        page = ordered[parsed.offset:parsed.offset + parsed.limit]
        summaries = [{key: value for key, value in element.items()
                      if key in {"id", "kind", "assetId", "x", "y", "width", "height"}}
                     for element in page]
        next_offset = parsed.offset + len(page)
        result = {"elements": summaries, "total": len(ordered), "offset": parsed.offset,
                  "nextOffset": next_offset if next_offset < len(ordered) else None,
                  "truncated": next_offset < len(ordered)}
    elif isinstance(parsed, GetElementsInput):
        if any(ident not in objects for ident in parsed.object_ids):
            raise ToolError("OBJECT_OUT_OF_SCOPE", "对象不属于本次运行的画布快照")
        result = {"elements": [dict(objects[ident]) for ident in parsed.object_ids]}
    else:
        result = _inspect_image(parsed.asset_id, snapshot, assets, Path(media_root))
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > max_output_bytes:
        raise ToolError("TOOL_OUTPUT_LIMIT", "工具结果超过大小限制，请减少查询的对象数量")
    return result
