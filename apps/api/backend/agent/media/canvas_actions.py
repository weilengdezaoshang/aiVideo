"""Deployment-independent canvas edit handoffs; never submit a provider generation."""
import re

from .catalog import HostError

CUTOUT = r"抠图|抠出|去背|(?:去除|去掉|移除|删除)背景|提取主体"


def is_cutout_request(message: str) -> bool:
    cleaned = re.sub(rf"(?:不要|不用|不需要|无需|别)\s*(?:{CUTOUT})", "", message)
    return bool(re.search(CUTOUT, cleaned))


def prepare_cutout(message: str, selected: list[str], objects: dict) -> dict:
    cleaned = re.sub(r"(?:不要|不需要|无需)\s*(?:视频|动画|短片)", "", message)
    if re.search(r"视频|动画|短片|\bvideo\b", cleaned, re.I):
        raise HostError("intent_clarify", "请先确认图片抠图，完成后再发送视频创作要求", status=422)
    if len(selected) != 1:
        raise HostError("intent_clarify", "请在画布选中一张图片，再发送抠图要求", status=422)
    ident = selected[0]
    source = objects.get(ident, {})
    if source.get("kind") != "image" or not source.get("assetId") or source.get("gen"):
        raise HostError("intent_clarify", "请选择一张已导入且生成完成的图片进行抠图", status=422)
    return {"type": "cutout", "sourceObjectId": ident, "sourceAssetId": source["assetId"]}


def validate_cutout_result(action: dict, result_id: str, objects: dict) -> str:
    result = objects.get(result_id, {})
    result = result if isinstance(result, dict) else {}
    lineage, cutout = result.get("lineage"), result.get("cutout")
    lineage = lineage if isinstance(lineage, dict) else {}
    cutout = cutout if isinstance(cutout, dict) else {}
    params = lineage.get("params")
    params = params if isinstance(params, dict) else {}
    if (result.get("kind") != "image" or not result.get("assetId") or
            lineage.get("fromId") != action["sourceObjectId"] or
            params.get("operation") != "cutout" or
            cutout.get("originalAssetId") != action["sourceAssetId"]):
        raise HostError("invalid_cutout_result", "尚未找到本次抠图的已保存结果", status=422)
    return result["assetId"]
