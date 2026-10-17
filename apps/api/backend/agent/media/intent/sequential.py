"""顺位只识别结构；每段的 L2 仍交给同一意图决策链。"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Sequence:
    parts: tuple[str, ...]
    strong: bool


def detect_sequence(message):
    # 否定尾句不是要求执行的第二步。
    if re.search(r'(?:暂时|先)?不要视频|别做成视频|只要抠图', message):
        return None
    strong = bool(re.match(r'\s*(?:先|first\b)', message, re.I))
    marker = r'[,，;；]?\s*(?:然后|之后|以后|再|\bthen\b|后直接|后(?=改|抠|视频|生成|换))(?:再)?\s*'
    if re.search(r'一键.*[+＋]', message):
        parts = re.sub(r'^一键[：: ]*', '', message).replace('＋', '+').split('+')
        return Sequence(tuple(parts), False)
    if not strong and not re.search(r'然后|之后|以后|完.*再|first.*then|后直接|\bthen\b', message, re.I):
        return None
    if re.search(marker, message, re.I):
        parts = re.split(marker, message, flags=re.I)
        parts[0] = re.sub(r'^\s*(?:先|first\b)\s*', '', parts[0], flags=re.I)
        return Sequence(tuple(part.strip(' ,，。;；') for part in parts), strong)
    return None
