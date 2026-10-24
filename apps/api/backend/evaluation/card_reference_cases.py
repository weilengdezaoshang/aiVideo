"""Labeled card-reference cases for the 30-case coreference gate. No model calls."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.core.card_reference import CardCandidate

FIXTURE_PATH = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "card-reference-cases.jsonl"
Category = Literal["at_name", "kind", "starred", "subject", "selection", "ambiguous", "unavailable", "delisted"]


class CardReferenceCase(BaseModel):
    """The resolver reads only `utterance`; cards carry the visible card list."""

    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    utterance: str = Field(min_length=1)
    category: Category
    cards: list[CardCandidate]
    expected_status: Literal["resolved", "ambiguous", "unavailable"]
    expected_card_id: str | None = None
    expected_reason: str = Field(min_length=1)


def _card(card_id, kind, name, *, description="", starred=False, selected=False, available=True,
          subject_category=""):
    return {"card_id": card_id, "card_kind": kind, "name": name, "description": description,
            "starred": starred, "selected": selected, "available": available,
            "subject_category": subject_category}


def _case(case_id, utterance, category, cards, status, reason, card=None) -> dict:
    return {"case_id": case_id, "utterance": utterance, "category": category, "cards": cards,
            "expected_status": status, "expected_card_id": card, "expected_reason": reason}


def case_rows() -> list[dict]:
    c = _card
    rows = [
        # @ 精确引用
        _case("card-at-style", "把 @水彩插画 的风格用到那张图上", "at_name",
              [c("k1", "style", "水彩插画"), c("k2", "style", "赛博朋克")],
              "resolved", "at_name", "k1"),
        _case("card-at-subject", "让 @橘子 出现在三张海报里", "at_name",
              [c("s1", "subject", "橘子", subject_category="猫")],
              "resolved", "at_name", "s1"),
        _case("card-at-miss", "@不存在的卡 生成一张", "unavailable",
              [c("s1", "subject", "橘子")], "unavailable", "at_name_not_found"),
        _case("card-at-dup", "@水彩 到底是哪张", "ambiguous",
              [c("a", "style", "水彩"), c("b", "subject", "水彩")],
              "ambiguous", "at_name_ambiguous"),
        _case("card-at-english", "@Pixel-art 生成一张海报", "at_name",
              [c("k1", "style", "Pixel-art")],
              "resolved", "at_name", "k1"),
        # 风格类
        _case("card-style-only", "把这张图的风格用到那张上", "kind",
              [c("k1", "style", "水彩插画")], "resolved", "style_kind", "k1"),
        _case("card-style-two", "这张图的风格换成另一个画风", "ambiguous",
              [c("k1", "style", "水彩插画"), c("k2", "style", "赛博朋克")],
              "ambiguous", "style_ambiguous"),
        _case("card-style-starred", "用这个风格再生成两张", "starred",
              [c("k1", "style", "水彩插画", starred=True), c("k2", "style", "赛博朋克")],
              "resolved", "style_starred", "k1"),
        _case("card-style-none", "用这个风格生成", "unavailable",
              [c("s1", "subject", "橘子")], "unavailable", "no_style_card"),
        _case("card-style-delisted", "这张图的风格再试一次", "delisted",
              [c("k1", "style", "水彩插画", available=False), c("k2", "style", "厚涂", starred=True)],
              "resolved", "style_kind", "k2"),
        _case("card-style-all-delisted", "用这个风格改一下", "unavailable",
              [c("k1", "style", "水彩插画", available=False)],
              "unavailable", "no_style_card"),
        # 角色与环境
        _case("card-character", "让这个角色站在雪地里", "kind",
              [c("r1", "character", "小明")], "resolved", "character_kind", "r1"),
        _case("card-character-two", "这个人物换套衣服", "ambiguous",
              [c("r1", "character", "小明"), c("r2", "character", "小红")],
              "ambiguous", "character_ambiguous"),
        _case("card-environment", "把主体放进这个场景里", "kind",
              [c("e1", "environment", "黄昏海滩")], "resolved", "environment_kind", "e1"),
        _case("card-environment-none", "换一个背景", "unavailable",
              [c("k1", "style", "水彩")], "unavailable", "no_environment_card"),
        # 主体指代（这只猫等）
        _case("card-subject-cat", "让这只猫出现在 3 张海报里", "subject",
              [c("s1", "subject", "橘子", subject_category="猫"),
               c("k1", "style", "水彩")],
              "resolved", "subject_match", "s1"),
        _case("card-subject-name", "这只猫再画一张侧脸", "subject",
              [c("s1", "subject", "橘猫团子", subject_category="猫"),
               c("s2", "subject", "柯基", subject_category="狗")],
              "resolved", "subject_match", "s1"),
        _case("card-subject-dog", "这只狗戴上帽子", "subject",
              [c("s1", "subject", "橘子", subject_category="猫"),
               c("s2", "subject", "柯基", subject_category="狗")],
              "resolved", "subject_match", "s2"),
        _case("card-subject-two-cats", "这只猫和另一只猫都要", "ambiguous",
              [c("s1", "subject", "橘子", subject_category="猫"),
               c("s2", "subject", "布丁", subject_category="猫")],
              "ambiguous", "subject_ambiguous"),
        _case("card-subject-delisted", "这只猫再生成一张", "delisted",
              [c("s1", "subject", "橘子", subject_category="猫", available=False),
               c("s2", "subject", "柯基", subject_category="狗")],
              "unavailable", "no_matching_card"),
        _case("card-subject-starred-fallback", "这个主体换个姿势", "starred",
              [c("s1", "subject", "橘子", starred=True), c("s2", "subject", "柯基")],
              "resolved", "subject_starred", "s1"),
        # 选中优先
        _case("card-selected", "就用我选中的这张卡", "selection",
              [c("s1", "subject", "橘子", selected=True), c("s2", "subject", "柯基")],
              "resolved", "selected", "s1"),
        _case("card-selected-beats-starred", "这个就用选中的", "selection",
              [c("k1", "style", "水彩", starred=True), c("k2", "style", "赛博", selected=True)],
              "resolved", "selected", "k2"),
        _case("card-selected-two", "选中的两张都用上", "ambiguous",
              [c("a", "subject", "橘子", selected=True), c("b", "subject", "柯基", selected=True)],
              "ambiguous", "multiple_selected"),
        _case("card-selected-none", "用选中的生成", "unavailable",
              [c("s1", "subject", "橘子")], "unavailable", "no_selection"),
        _case("card-selected-delisted", "用选中的那张卡", "delisted",
              [c("s1", "subject", "橘子", selected=True, available=False),
               c("k1", "style", "水彩")],
              "unavailable", "no_selection"),
        # 兜底与无指代
        _case("card-bare-starred", "这个再来一张", "starred",
              [c("s1", "subject", "橘子", starred=True), c("k1", "style", "水彩")],
              "resolved", "starred", "s1"),
        _case("card-bare-many", "这个重新画", "ambiguous",
              [c("s1", "subject", "橘子"), c("k1", "style", "水彩")],
              "unavailable", "no_matching_card"),
        _case("card-empty", "就按刚才说的生成", "unavailable",
              [], "unavailable", "no_reference_intent"),
        _case("card-no-intent", "生成一张赛博朋克城市的图", "unavailable",
              [c("k1", "style", "水彩"), c("s1", "subject", "橘子")],
              "unavailable", "no_reference_intent"),
    ]
    return rows


def load_cases(path: Path | None = None) -> list[CardReferenceCase]:
    source = path or FIXTURE_PATH
    cases = []
    seen = set()
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = CardReferenceCase.model_validate_json(line)
        if case.case_id in seen:
            raise ValueError(f"duplicate card reference case {case.case_id}")
        seen.add(case.case_id)
        cases.append(case)
    if len(cases) < 30:
        raise ValueError("card reference cases must contain at least 30 rows")
    return cases


def write_fixture(path: Path | None = None) -> Path:
    target = path or FIXTURE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in case_rows()]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_cases(target)
    return target
