"""Read-only canvas tools: frozen projections in, measured observations out."""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.agent.contracts import Coverage, FailureDetails, NextAction, Omitted, Page
from backend.agent.evidence import KIND_LABELS, RawFact, MAX_ITEM_FACTS
from backend.agent.tools.errors import ToolError, ToolInput
from backend.agent.tools.registry import Outcome, ToolContext, ToolSpec


ElementKind = Literal["image", "video", "placeholder", "error", "board", "text", "draft"]
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000


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


LIST_KEYS = {"id", "kind", "name", "assetId", "x", "y", "width", "height"}
READ_STAGES = frozenset({"plan", "execute"})


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def _label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def _image_asset_calls(elements: list[dict], limit: int = 3) -> list[NextAction]:
    seen, calls = set(), []
    for element in elements:
        asset = element.get("assetId")
        if element.get("kind") == "image" and isinstance(asset, str) and asset and asset not in seen:
            seen.add(asset)
            calls.append(NextAction(action="call_tool", tool="inspect_image", arguments={"assetId": asset},
                reason="读取该图片原文件的尺寸与格式", priority="suggested"))
        if len(calls) == limit:
            break
    return calls


def _read_canvas(parsed, context: ToolContext, keep) -> Outcome:
    snapshot, counts = context.snapshot, {}
    for element in snapshot["objects"].values():
        counts[element["kind"]] = counts.get(element["kind"], 0) + 1
    data = {"documentId": snapshot["id"], "name": snapshot["name"], "revision": snapshot["revision"],
            "objectCount": len(snapshot["objects"]), "countsByKind": counts,
            "selectedObjectIds": snapshot["selectedObjectIds"]}
    return Outcome(data=data, coverage=Coverage(complete=True))


def _read_canvas_facts(outcome: Outcome, parsed) -> list[RawFact]:
    data = outcome.data
    facts = [RawFact("画布保存版本号", "data.revision", data["revision"], "complete"),
             RawFact("画布对象总数", "data.objectCount", data["objectCount"], "complete")]
    for kind, count in data["countsByKind"].items():
        facts.append(RawFact(f"{_label(kind)}对象数量", f"data.countsByKind.{kind}", count, "complete"))
    facts.append(RawFact("发送时选中的对象数量", "data.selectedObjectIds.length",
                         len(data["selectedObjectIds"]), "complete"))
    return facts


def _read_canvas_next(outcome: Outcome, parsed) -> list[NextAction]:
    data, suggestions = outcome.data, []
    if data["selectedObjectIds"]:
        suggestions.append(NextAction(action="call_tool", tool="get_canvas_elements",
            arguments={"objectIds": data["selectedObjectIds"][:50]},
            reason="查看发送时选中对象的详情", priority="suggested"))
    if data["countsByKind"].get("image"):
        suggestions.append(NextAction(action="call_tool", tool="list_canvas_elements",
            arguments={"kind": "image"}, reason="列出画布上的图片对象", priority="suggested"))
    return suggestions


def _list_elements(parsed: ListElementsInput, context: ToolContext, keep) -> Outcome:
    objects, snapshot = context.snapshot["objects"], context.snapshot
    ordered = [objects[ident] for ident in snapshot["order"]
               if parsed.kind is None or objects[ident]["kind"] == parsed.kind]
    page = ordered[parsed.offset:parsed.offset + parsed.limit]
    if keep is not None:
        page = page[:keep]
    summaries = [{key: value for key, value in element.items() if key in LIST_KEYS} for element in page]
    end = parsed.offset + len(page)
    more = end < len(ordered)
    complete = parsed.offset == 0 and not more
    reason = None if complete else f"本次仅覆盖第 {min(parsed.offset + 1, len(ordered)) if page else 0}"\
        f"–{end} 个，共 {len(ordered)} 个"
    return Outcome(data={"elements": summaries}, coverage=Coverage(complete=complete, reason=reason),
        page=Page(offset=parsed.offset, nextOffset=end if more else None, total=len(ordered)),
        count=len(page))


def _list_facts(outcome: Outcome, parsed: ListElementsInput) -> list[RawFact]:
    elements, page = outcome.data["elements"], outcome.page
    scope = f"{_label(parsed.kind)}" if parsed.kind else ""
    facts = [RawFact(f"{scope}对象总数", "page.total", page.total, "complete"),
             RawFact(f"本页返回的对象数量（起始偏移 {page.offset}）", "data.elements.length", len(elements),
                     "partial" if page.nextOffset is not None or page.offset else "complete")]
    for index, element in enumerate(elements[:MAX_ITEM_FACTS]):
        facts.append(RawFact(f"本页第 {index + 1} 个对象为{_label(element['kind'])}，尺寸 "
            f"{_num(element['width'])}×{_num(element['height'])}", f"data.elements[{index}]",
            [element["width"], element["height"]], "item"))
    return facts


def _list_next(outcome: Outcome, parsed: ListElementsInput) -> list[NextAction]:
    page, elements, suggestions = outcome.page, outcome.data["elements"], []
    if page.nextOffset is not None:
        arguments = {"offset": page.nextOffset, "limit": parsed.limit}
        if parsed.kind:
            arguments["kind"] = parsed.kind
        suggestions.append(NextAction(action="call_tool", tool="list_canvas_elements", arguments=arguments,
            reason=f"还有 {page.total - page.nextOffset} 个对象未列出", priority="required"))
    suggestions.extend(_image_asset_calls(elements))
    if elements:
        suggestions.append(NextAction(action="call_tool", tool="get_canvas_elements",
            arguments={"objectIds": [element["id"] for element in elements[:10]]},
            reason="读取这些对象的文字与样式详情", priority="suggested"))
    return suggestions


def _get_elements(parsed: GetElementsInput, context: ToolContext, keep) -> Outcome:
    objects = context.snapshot["objects"]
    unknown = [ident for ident in parsed.object_ids if ident not in objects]
    if unknown:
        raise ToolError("OBJECT_OUT_OF_SCOPE", "对象不属于本次运行的画布快照",
            FailureDetails(unknownObjectIds=unknown[:50]))
    requested = list(dict.fromkeys(parsed.object_ids))
    returned = requested if keep is None else requested[:keep]
    omitted = requested[len(returned):]
    complete = not omitted
    return Outcome(data={"elements": [dict(objects[ident]) for ident in returned]},
        coverage=Coverage(complete=complete, reason=None if complete else
            f"因输出大小限制，{len(omitted)} 个对象未返回", missing=None if complete else Omitted(objectIds=omitted)),
        omitted=Omitted(objectIds=omitted), count=len(returned),
        asset_ids=tuple(dict.fromkeys(e["assetId"] for ident in returned
                                      if isinstance(e := objects[ident].get("assetId") and objects[ident], dict))))


def _get_facts(outcome: Outcome, parsed: GetElementsInput) -> list[RawFact]:
    elements = outcome.data["elements"]
    facts = [RawFact("本次返回的对象数量", "data.elements.length", len(elements),
                     "complete" if outcome.coverage.complete else "partial")]
    for index, element in enumerate(elements[:MAX_ITEM_FACTS]):
        facts.append(RawFact(f"返回结果中第 {index + 1} 个对象为{_label(element['kind'])}，位置 "
            f"({_num(element['x'])},{_num(element['y'])})，尺寸 {_num(element['width'])}×{_num(element['height'])}",
            f"data.elements[{index}]", [element["x"], element["y"], element["width"], element["height"]], "item"))
        text = element.get("text")
        if isinstance(text, str) and 0 < len(text) <= 200:
            facts.append(RawFact(f"返回结果中第 {index + 1} 个对象的文字内容", f"data.elements[{index}].text",
                                 text, "item"))
    return facts


def _get_next(outcome: Outcome, parsed: GetElementsInput) -> list[NextAction]:
    suggestions = []
    missing = outcome.omitted.objectIds
    if missing:
        suggestions.append(NextAction(action="call_tool", tool="get_canvas_elements",
            arguments={"objectIds": missing[:50]}, reason=f"还有 {len(missing)} 个对象未返回",
            priority="required"))
    suggestions.extend(_image_asset_calls(outcome.data["elements"]))
    return suggestions


def _inspect(parsed: InspectImageInput, context: ToolContext, keep) -> Outcome:
    data = _inspect_image(parsed.asset_id, context.snapshot, context.assets, context.media_root)
    return Outcome(data=data, coverage=Coverage(complete=True), asset_ids=(parsed.asset_id,))


def _inspect_facts(outcome: Outcome, parsed: InspectImageInput) -> list[RawFact]:
    data = outcome.data
    return [RawFact(f"图片尺寸为 {data['width']}×{data['height']} 像素", "data", [data["width"], data["height"]], "item"),
            RawFact("图片文件格式", "data.format", data["format"], "item"),
            RawFact("图片文件大小（字节）", "data.bytes", data["bytes"], "item"),
            RawFact("文件哈希与本次运行绑定的素材一致", "data.sha256", data["sha256"], "item")]


def _no_next(outcome: Outcome, parsed) -> list[NextAction]:
    return []


READ_TOOLS = [
    ToolSpec("read_canvas", 1, "读取本次运行冻结的画布版本、对象数量和发送时选中对象。", ReadCanvasInput,
             "read", READ_STAGES, 5, _read_canvas, _read_canvas_facts, _read_canvas_next),
    ToolSpec("list_canvas_elements", 2, "按画布顺序分页列出对象摘要，可按类型筛选。", ListElementsInput,
             "read", READ_STAGES, 5, _list_elements, _list_facts, _list_next),
    ToolSpec("get_canvas_elements", 2, "获取指定对象的文字、布局属性和素材关联。", GetElementsInput,
             "read", READ_STAGES, 5, _get_elements, _get_facts, _get_next),
    ToolSpec("inspect_image", 1, "读取画布关联的真实图片文件，测量尺寸、格式、大小和哈希；不理解图片内容。",
             InspectImageInput, "read", READ_STAGES, 15, _inspect, _inspect_facts, _no_next),
]
