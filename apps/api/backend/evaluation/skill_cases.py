"""Labeled skill-playbook cases; selection messages verified against the matcher."""
from __future__ import annotations

import json
from pathlib import Path


FIXTURE_PATH = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "skill-cases.jsonl"

ECO, POSTER, STORY = "电商主图", "海报系列", "分镜短片"


def _step(i, tool, deps=(), prompt=None):
    return {"id": f"t{i}", "kind": "call_tool", "tool": tool,
            "params": {"prompt": prompt or f"第{i}步"},
            "depends_on": [f"t{d}" for d in deps]}


def _ask(i, deps=()):
    return {"id": f"t{i}", "kind": "ask_user", "params": {"question": "确认"},
            "depends_on": [f"t{d}" for d in deps]}


def _plan(*steps, goal="完成创作"):
    return {"goal": goal, "steps": list(steps)}


def _case(case_id, skill, category, user_message, plan, label_pass, *,
          explicit=None, expected=None, codes=()) -> dict:
    return {"case_id": case_id, "skill": skill, "category": category,
        "user_message": user_message, "explicit_skill": explicit,
        "expected_skill": expected, "plan": plan, "label_pass": label_pass,
        "expected_codes": list(codes)}


# Shared plan shapes.
ECO_FULL = _plan(_step(1, "detect_subject"), _ask(2, (1,)), _step(3, "cutout", (1, 2)),
                 _ask(4, (3,)), _step(5, "image_edit_preview", (3, 4)))
POS_FULL = _plan(_step(1, "text_to_image_preview", prompt="方向一"),
                 _step(2, "text_to_image_preview", prompt="方向二"),
                 _ask(3, (1, 2)), _step(4, "text_to_image_final", (3,), prompt="正片一"),
                 _step(5, "text_to_image_final", (3,), prompt="正片二"))
SB_FULL = _plan(_step(1, "create_asset_card"), _step(2, "text_to_image_preview", (1,)),
                _ask(3, (2,)), _step(4, "image_to_video_preview", (3,)), _step(5, "video_concat", (4,)))


def case_rows() -> list[dict]:
    rows = [
        # ---- 电商主图 15 ----
        _case("eco-sel-rule", "ecommerce", "selection", "给这张产品图做白底图和换背景", ECO_FULL,
              True, expected=ECO),
        _case("eco-sel-explicit", "ecommerce", "selection", "做一组活动海报", ECO_FULL, True,
              explicit=ECO, expected=ECO),
        _case("eco-sel-none", "ecommerce", "none", "随便画一只猫",
              _plan(_step(1, "text_to_image_preview")), True),
        _case("eco-allowlist-video", "ecommerce", "allowlist", "电商主图换背景",
              _plan(_step(1, "text_to_video_preview")), False, expected=ECO, codes=["tool_out_of_scope"]),
        _case("eco-allowlist-update-card", "ecommerce", "allowlist", "给这张产品图做白底图和换背景",
              _plan(_step(1, "detect_subject"), _step(2, "cutout", (1,)), _ask(3, (2,)),
                    _step(4, "image_edit_preview", (2, 3)), _step(5, "update_asset_card")),
              False, expected=ECO, codes=["tool_out_of_scope"]),
        _case("eco-phase-missing-cutout", "ecommerce", "phase", "给这张产品图做白底图和换背景",
              _plan(_step(1, "image_edit_preview")), False, expected=ECO, codes=["skill_phase_missing"]),
        _case("eco-phase-missing-bg", "ecommerce", "phase", "给这张产品图做白底图和换背景",
              _plan(_step(1, "detect_subject"), _ask(2, (1,)), _step(3, "cutout", (1, 2))),
              False, expected=ECO, codes=["skill_phase_missing"]),
        _case("eco-confirm-missing", "ecommerce", "confirmation", "给这张产品图做白底图和换背景",
              _plan(_step(1, "detect_subject"), _step(2, "cutout", (1,)),
                    _step(3, "image_edit_preview", (2,))),
              False, expected=ECO, codes=["skill_confirmation_missing"]),
        _case("eco-confirm-late", "ecommerce", "confirmation", "给这张产品图做白底图和换背景",
              _plan(_step(1, "detect_subject"), _step(2, "cutout", (1,)),
                    _step(3, "image_edit_preview", (1, 2)), _ask(4, (3,))),
              False, expected=ECO, codes=["skill_confirmation_missing"]),
        _case("eco-good-full", "ecommerce", "good", "给这张产品图做白底图和换背景", ECO_FULL, True,
              expected=ECO),
        _case("eco-good-multisize", "ecommerce", "good", "给这张产品图做白底图和换背景",
              _plan(*ECO_FULL["steps"], _step(6, "image_edit_final", (5,), prompt="竖版尺寸")), True,
              expected=ECO),
        _case("eco-good-cards", "ecommerce", "good", "给这张产品图做白底图和换背景",
              _plan(_step(1, "detect_subject"), _step(2, "image_analyze", (1,)),
                    _step(3, "create_asset_card", (2,)), _ask(4, (3,)),
                    _step(5, "cutout", (1, 4)), _ask(6, (5,)), _step(7, "image_edit_preview", (5, 6))),
              True, expected=ECO),
        _case("eco-sel-rule-2", "ecommerce", "selection", "帮我把产品做成多尺寸铺货主图", ECO_FULL,
              True, expected=ECO),
        _case("eco-phase-missing-both", "ecommerce", "phase", "给这张产品图做白底图和换背景",
              _plan(_step(1, "image_analyze")), False, expected=ECO, codes=["skill_phase_missing"]),
        _case("eco-none-code", "ecommerce", "none", "把这段代码重构一下",
              _plan(_step(1, "text_to_image_preview")), True),

        # ---- 海报系列 15 ----
        _case("pos-sel-rule", "poster", "selection", "做一组活动海报", POS_FULL, True, expected=POSTER),
        _case("pos-sel-explicit", "poster", "selection", "把这个剧本拍成短视频，三个镜头拼接", POS_FULL,
              True, explicit=POSTER, expected=POSTER),
        _case("pos-allowlist-cutout", "poster", "allowlist", "做一组活动海报",
              _plan(_step(1, "cutout")), False, expected=POSTER, codes=["tool_out_of_scope"]),
        _case("pos-allowlist-edit", "poster", "allowlist", "海报系列风格统一出图",
              _plan(_step(1, "image_edit_preview")), False, expected=POSTER, codes=["tool_out_of_scope"]),
        _case("pos-phase-missing-final", "poster", "phase", "做一组活动海报",
              _plan(_step(1, "text_to_image_preview")), False, expected=POSTER,
              codes=["skill_phase_missing"]),
        _case("pos-phase-missing-draft", "poster", "phase", "做一组活动海报",
              _plan(_step(1, "text_to_image_final"), _step(2, "text_to_image_final")),
              False, expected=POSTER, codes=["skill_phase_missing"]),
        _case("pos-confirm-missing", "poster", "confirmation", "做一组活动海报",
              _plan(_step(1, "text_to_image_preview", prompt="方向一"),
                    _step(2, "text_to_image_preview", prompt="方向二"),
                    _step(3, "text_to_image_final", (1, 2), prompt="正片一"),
                    _step(4, "text_to_image_final", (1, 2), prompt="正片二")),
              False, expected=POSTER, codes=["skill_confirmation_missing"]),
        _case("pos-confirm-one-sided", "poster", "confirmation", "做一组活动海报",
              _plan(_step(1, "text_to_image_preview", prompt="方向一"), _ask(2, (1,)),
                    _step(3, "text_to_image_preview", prompt="方向二"),
                    _step(4, "text_to_image_final", (3,))),
              False, expected=POSTER, codes=["skill_confirmation_missing"]),
        _case("pos-good-two-directions", "poster", "good", "做一组活动海报", POS_FULL, True,
              expected=POSTER),
        _case("pos-good-card", "poster", "good", "海报系列风格统一出图",
              _plan(_step(1, "create_asset_card"), _step(2, "text_to_image_preview", (1,), prompt="方向一"),
                    _step(3, "text_to_image_preview", (1,), prompt="方向二"), _ask(4, (2, 3)),
                    _step(5, "text_to_image_final", (4,), prompt="正片一")),
              True, expected=POSTER),
        _case("pos-sel-rule-2", "poster", "selection", "海报系列风格统一出图", POS_FULL, True,
              expected=POSTER),
        _case("pos-none-code", "poster", "none", "把这段代码重构一下",
              _plan(_step(1, "text_to_image_preview")), True),
        _case("pos-allowlist-video", "poster", "allowlist", "做一组活动海报",
              _plan(_step(1, "text_to_video_preview")), False, expected=POSTER, codes=["tool_out_of_scope"]),
        _case("pos-good-update-card", "poster", "good", "做一组活动海报",
              _plan(_step(1, "text_to_image_preview"), _ask(2, (1,)),
                    _step(3, "update_asset_card", (2,)), _step(4, "text_to_image_final", (2, 3))),
              True, expected=POSTER),
        _case("pos-sel-city-poster", "poster", "selection", "生成一张赛博朋克城市海报", POS_FULL, True,
              expected=POSTER),

        # ---- 分镜短片 15 ----
        _case("sb-sel-rule", "storyboard", "selection", "把这个剧本拍成短视频，三个镜头拼接", SB_FULL,
              True, expected=STORY),
        _case("sb-sel-explicit", "storyboard", "selection", "电商主图换背景", SB_FULL, True,
              explicit=STORY, expected=STORY),
        _case("sb-allowlist-cutout", "storyboard", "allowlist", "多镜头拼接成片",
              _plan(_step(1, "cutout")), False, expected=STORY, codes=["tool_out_of_scope"]),
        _case("sb-allowlist-edit", "storyboard", "allowlist", "把这个剧本拍成短视频，三个镜头拼接",
              _plan(_step(1, "image_edit_preview")), False, expected=STORY, codes=["tool_out_of_scope"]),
        _case("sb-phase-missing-video", "storyboard", "phase", "把这个剧本拍成短视频，三个镜头拼接",
              _plan(_step(1, "create_asset_card"), _step(2, "text_to_image_preview", (1,)),
                    _ask(3, (2,)), _step(4, "video_concat", (3,))),
              False, expected=STORY, codes=["skill_phase_missing"]),
        _case("sb-phase-missing-concat", "storyboard", "phase", "把这个剧本拍成短视频，三个镜头拼接",
              _plan(_step(1, "create_asset_card"), _step(2, "text_to_image_preview", (1,)),
                    _ask(3, (2,)), _step(4, "image_to_video_preview", (3,))),
              False, expected=STORY, codes=["skill_phase_missing"]),
        _case("sb-phase-missing-storyboard", "storyboard", "phase", "把这个剧本拍成短视频，三个镜头拼接",
              _plan(_step(1, "create_asset_card"), _step(2, "image_to_video_preview", (1,)),
                    _step(3, "video_concat", (2,))),
              False, expected=STORY, codes=["skill_phase_missing"]),
        _case("sb-phase-missing-assets", "storyboard", "phase", "把这个剧本拍成短视频，三个镜头拼接",
              _plan(_step(1, "text_to_image_preview"), _ask(2, (1,)),
                    _step(3, "image_to_video_preview", (2,)), _step(4, "video_concat", (3,))),
              False, expected=STORY, codes=["skill_phase_missing"]),
        _case("sb-confirm-missing", "storyboard", "confirmation", "把这个剧本拍成短视频，三个镜头拼接",
              _plan(_step(1, "create_asset_card"), _step(2, "text_to_image_preview", (1,)),
                    _step(3, "image_to_video_preview", (2,)), _step(4, "video_concat", (3,))),
              False, expected=STORY, codes=["skill_confirmation_missing"]),
        _case("sb-confirm-late", "storyboard", "confirmation", "把这个剧本拍成短视频，三个镜头拼接",
              _plan(_step(1, "create_asset_card"), _step(2, "text_to_image_preview", (1,)),
                    _step(3, "image_to_video_preview", (2,)), _ask(4, (3,)), _step(5, "video_concat", (4,))),
              False, expected=STORY, codes=["skill_confirmation_missing"]),
        _case("sb-good-text2video", "storyboard", "good", "把这个剧本拍成短视频，三个镜头拼接",
              _plan(_step(1, "create_asset_card"), _step(2, "text_to_image_preview", (1,)),
                    _ask(3, (2,)), _step(4, "text_to_video_preview", (3,)), _step(5, "video_concat", (4,))),
              True, expected=STORY),
        _case("sb-good-frames", "storyboard", "good", "把这个剧本拍成短视频，三个镜头拼接",
              _plan(_step(1, "create_asset_card"), _step(2, "text_to_image_preview", (1,)),
                    _ask(3, (2,)), _step(4, "frames_to_video_preview", (3,)),
                    _step(5, "video_concat", (4,))),
              True, expected=STORY),
        _case("sb-good-two-shots", "storyboard", "good", "把这个剧本拍成短视频，三个镜头拼接",
              _plan(_step(1, "create_asset_card"), _step(2, "text_to_image_preview", (1,), prompt="镜头一"),
                    _step(3, "text_to_image_preview", (1,), prompt="镜头二"), _ask(4, (2, 3)),
                    _step(5, "image_to_video_preview", (4,), prompt="镜头一"),
                    _step(6, "image_to_video_preview", (4,), prompt="镜头二"),
                    _step(7, "video_concat", (5, 6))),
              True, expected=STORY),
        _case("sb-sel-rule-2", "storyboard", "selection", "多镜头拼接成片", SB_FULL, True,
              expected=STORY),
        _case("sb-none-cat", "storyboard", "none", "随便画一只猫",
              _plan(_step(1, "text_to_image_preview")), True),
    ]
    return rows


def load_cases(path: Path | None = None) -> list:
    """Parsed SkillCase rows; every official skill suite must carry >=15 cases."""
    from .skill_evaluation import SkillCase
    source = path or FIXTURE_PATH
    cases = []
    seen = set()
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = SkillCase.model_validate_json(line)
        if case.case_id in seen:
            raise ValueError(f"duplicate skill case {case.case_id}")
        seen.add(case.case_id)
        cases.append(case)
    counts = {}
    for case in cases:
        counts[case.skill] = counts.get(case.skill, 0) + 1
    if any(count < 15 for count in counts.values()) or set(counts) != {"ecommerce", "poster", "storyboard"}:
        raise ValueError(f"each official skill needs >=15 cases, got {counts}")
    return cases


def write_fixture(path: Path | None = None) -> Path:
    target = path or FIXTURE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in case_rows()]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_cases(target)
    return target
