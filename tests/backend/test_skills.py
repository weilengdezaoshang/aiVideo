"""Skill playbook unit coverage: parsing, selection, compiled rules, drafting, gate."""
import pytest

from backend.core.skills.compile_rules import skill_intent_rules
from backend.core.skills.contracts import SkillPhase, SkillRouting, SkillSpec, parse_skill_markdown
from backend.core.skills.draft import extract_defaults, fill_parameters
from backend.core.skills.selection import SkillSettings, SkillView, match_skill
from backend.core.skills.store import seed_official_skills
from backend.evaluation.skill_cases import case_rows, load_cases
from backend.evaluation.skill_evaluation import evaluate_cases, official_specs


def _spec(**overrides):
    base = dict(name="测试技能", version="1", scenes=["电商产品主图", "白底图"],
        role="测试", body="正文", allowlist=["detect_subject", "cutout", "image_edit_preview"],
        phases=[SkillPhase(name="抠图", tools=["detect_subject", "cutout"], required=True, confirm=True),
                SkillPhase(name="出图", tools=["image_edit_preview"], required=True)],
        routing=SkillRouting(ask_first=["背景未定"], act=["给了产品图"]),
        boundaries=["不做排版"], defaults={"count": 1})
    base.update(overrides)
    return SkillSpec(**base)


MARKDOWN = """---
name: 电商主图
version: "1"
scenes:
  - 电商产品主图
role: 电商视觉师
allowlist:
  - detect_subject
  - cutout
phases:
  - name: 抠主体
    tools: [detect_subject, cutout]
    required: true
    confirm: true
---
# 手册正文
阶段详述。
"""


def test_parse_markdown_frontmatter_and_body():
    spec = parse_skill_markdown(MARKDOWN)
    assert spec.name == "电商主图"
    assert spec.allowlist == ["detect_subject", "cutout"]
    assert spec.phases[0].confirm is True
    assert spec.body.startswith("# 手册正文")


def test_parse_rejects_malformed_definitions():
    with pytest.raises(ValueError, match="frontmatter"):
        parse_skill_markdown("no frontmatter here")
    with pytest.raises(ValueError, match="not closed"):
        parse_skill_markdown("---\nname: x\n")
    broken = MARKDOWN.replace("scenes:", "scenes_x:")
    with pytest.raises(ValueError, match="invalid skill"):
        parse_skill_markdown(broken)
    MARKDOWN.replace("- image_edit", "- text_to_video").replace(
        "phases:", "phases_placeholder:").replace(
        "phases_placeholder:", "phases:")
    with pytest.raises(ValueError, match="outside the allowlist"):
        SkillSpec.model_validate({
            "name": "x", "version": "1", "scenes": ["s"], "role": "r",
            "allowlist": ["cutout"],
            "phases": [{"name": "p", "tools": ["detect_subject"]}]})


def test_content_hash_ignores_source_only():
    official = _spec()
    personal = _spec(source="user")
    assert official.content_hash() == personal.content_hash()
    assert _spec(name="另一个").content_hash() != official.content_hash()


def _views():
    specs = official_specs()
    return {name: spec for name, spec in specs.items()}, \
        [SkillView.of(name, name, spec) for name, spec in specs.items()]


def test_selection_priority_explicit_over_rules_over_none():
    specs, views = _views()
    picked = match_skill(views, "做一组活动海报", explicit="电商主图")
    assert picked.name == "电商主图"
    picked = match_skill(views, "给这张产品图做白底图和换背景")
    assert picked.name == "电商主图"
    assert match_skill(views, "随便聊两句") is None
    assert match_skill(views, "任何消息", explicit="不存在的技能") is None


def test_rule_threshold_gates_partial_overlap():
    # 「给这张产品做主图」只覆盖「电商产品主图」的部分二元组（≈0.4）。
    settings = SkillSettings(rule_threshold=0.6, retrieval_threshold=10 ** 6)
    _, views = _views()
    assert match_skill(views, "给这张产品做主图", settings=settings) is None


def _plan_view(steps, context_tools, spec=None):
    from backend.core.review import validate
    from backend.core.review.contracts import ToolSpec
    from backend.evaluation.skill_evaluation import TOOL_TABLE
    context = {"user_id": "u", "user_credits": 10 ** 6, "run_budget": 10 ** 6,
        "scope": {"tools": list(context_tools), "readable_assets": [], "writable_regions": []},
        "version_lock": {"tools": {name: ToolSpec(**tool.model_dump())
            for name, tool in TOOL_TABLE.items()}},
        "invariants": {}, "canvas": {"assets": [], "nodes": []}}
    return validate({"goal": "g", "steps": steps}, context,
        intent_extensions=skill_intent_rules(spec) if spec else None)


def test_compiled_rules_reject_missing_phase_and_late_confirmation():
    spec = _spec()
    skill_intent_rules(spec)
    # 出图阶段工具未出现 → skill_phase_missing
    result = _plan_view([{"id": "t1", "kind": "call_tool", "tool": "detect_subject",
                          "params": {"prompt": "x"}, "depends_on": []}], spec.allowlist, spec)
    assert [r.code for r in result.reasons if r.code.startswith("skill_")] == ["skill_phase_missing"]
    # 出图步骤未经过确认 ask → skill_confirmation_missing
    result = _plan_view([
        {"id": "t1", "kind": "call_tool", "tool": "detect_subject", "params": {"prompt": "x"}, "depends_on": []},
        {"id": "t2", "kind": "call_tool", "tool": "cutout", "params": {"prompt": "y"}, "depends_on": ["t1"]},
        {"id": "t3", "kind": "call_tool", "tool": "image_edit_preview", "params": {"prompt": "z"}, "depends_on": ["t2"]},
    ], spec.allowlist, spec)
    assert "skill_confirmation_missing" in [r.code for r in result.reasons]
    # 确认在抠图之后、出图之前 → 通过
    result = _plan_view([
        {"id": "t1", "kind": "call_tool", "tool": "detect_subject", "params": {"prompt": "x"}, "depends_on": []},
        {"id": "t2", "kind": "ask_user", "params": {}, "depends_on": ["t1"]},
        {"id": "t3", "kind": "call_tool", "tool": "image_edit_preview", "params": {"prompt": "z"}, "depends_on": ["t2"]},
    ], spec.allowlist, spec)
    assert result.verdict == "pass"


def test_allowlist_gate_rejects_outside_tool():
    spec = _spec()
    result = _plan_view([{"id": "t1", "kind": "call_tool", "tool": "text_to_video",
                          "params": {"prompt": "v"}, "depends_on": []}], spec.allowlist)
    assert "tool_out_of_scope" in [r.code for r in result.reasons]


def test_extract_defaults_templates_values():
    steps = [
        {"tool": "text_to_image_preview", "kind": "call_tool",
         "parameters": {"prompt": "一只橘猫海报", "count": 1, "width": 512, "height": 512}, "depends_on": []},
        {"tool": "text_to_image_final", "kind": "call_tool",
         "parameters": {"prompt": "正式版", "count": 1, "width": 512, "height": 768}, "depends_on": []},
    ]
    templated, defaults = extract_defaults(steps)
    assert templated[0]["parameters"]["prompt"] == "{{prompt}}"
    assert templated[1]["parameters"]["prompt"] == "{{prompt_2}}"
    assert templated[0]["parameters"]["count"] == "{{count}}"
    assert templated[1]["parameters"]["height"] == "{{height_2}}"  # 同名不同值分槽
    assert templated[1]["parameters"]["width"] == "{{width}}"      # 同值共享
    assert defaults["{{prompt}}"] == "一只橘猫海报"
    filled = fill_parameters(templated, {**defaults, "{{prompt}}": "一只柯基海报",
                                         "{{prompt_2}}": "正式版"})
    assert filled[0]["parameters"]["prompt"] == "一只柯基海报"
    assert filled[0]["parameters"]["count"] == 1
    with pytest.raises(ValueError, match="missing slot"):
        fill_parameters(templated, {})


def test_official_bundles_parse_and_carry_allowlists():
    specs = seed_official_skills()
    assert {spec.name for _, spec in specs} == {"电商主图", "海报系列", "分镜短片"}
    for _, spec in specs:
        assert spec.phases and spec.allowlist
        assert skill_intent_rules(spec)


def test_gate_fixture_matches_inline_and_all_pass():
    cases = load_cases()
    assert len(cases) == len(case_rows()) == 45
    summary = evaluate_cases(cases)
    assert summary["failed"] == 0, [g for g in summary["grades"] if not g["passed"]]
    assert all(bucket["total"] >= 15 for bucket in summary["by_skill"].values())
