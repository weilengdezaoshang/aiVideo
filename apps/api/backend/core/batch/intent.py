"""Decide when a frozen user request is an explicit batch. No model call."""
from __future__ import annotations

import re

_BATCH_WORDS = re.compile(r"批量|全部|每张|逐张|每一张|每个尺寸|每种尺寸")
_NEGATE = re.compile(r"不要批量|别批量|不用批量|无需批量")
_COMPARE = re.compile(r"比较|对比|哪个更好|哪张更好")
_SIZES = (
    (re.compile(r"方图|正方形|方形"), "feed_square"),
    (re.compile(r"竖版|故事版|故事尺寸"), "story_portrait"),
    (re.compile(r"横版|宽屏|横幅"), "banner_landscape"),
)


def apply_batch_intent(request: dict) -> dict:
    """Set a controlled ``batch`` flag and a frozen size list. Does not drop the selection."""

    if not isinstance(request, dict):
        return request
    message = str(request.get("message") or "")
    selected = list(request.get("selected_assets") or [])
    sizes = _named_sizes(message)
    if sizes and not request.get("sizes"):
        request["sizes"] = sizes
    if request.get("batch") is True:
        return request
    if _NEGATE.search(message):
        request["batch"] = False
        return request
    action = str(request.get("action") or "")
    if action in {"batch", "for_each"}:
        request["batch"] = True
        return request
    if _COMPARE.search(message):
        return request
    multi = len(selected) >= 2 and bool(_BATCH_WORDS.search(message))
    multi_size = len(sizes) >= 2
    if multi or multi_size:
        request["batch"] = True
    return request


def _named_sizes(message: str) -> list[str]:
    found = []
    for pattern, ident in _SIZES:
        if pattern.search(message) and ident not in found:
            found.append(ident)
    return found
