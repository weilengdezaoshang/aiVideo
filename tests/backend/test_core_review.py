"""Block 10 reviewer: every rule gets a pass case and a reject case; pure function, no I/O."""

import time

import pytest

from backend.core.review import (
    ParamRule,
    ReviewResult,
    RunContext,
    ToolSpec,
    intent_reason,
    validate,
)


def catalog(**overrides):
    tools = {
        "text_to_image": ToolSpec(
            id="text_to_image",
            version="1.2.0",
            capability="image_generation",
            output_type="image",
            input_types={},
            params_schema={
                "prompt": ParamRule(type="string", required=True),
                "aspect_ratio": ParamRule(type="string", enum=["9:16", "16:9", "1:1"]),
                "count": ParamRule(type="number", minimum=1, maximum=4),
                "width": ParamRule(type="number", minimum=1),
                "height": ParamRule(type="number", minimum=1),
            },
            cost_credits=20,
        ),
        "image_edit": ToolSpec(
            id="image_edit",
            version="1.1.0",
            capability="image_edit",
            output_type="image",
            input_types={"image": ["image"]},
            params_schema={
                "prompt": ParamRule(type="string", required=True),
                "aspect_ratio": ParamRule(type="string", enum=["9:16", "16:9", "1:1"]),
            },
            cost_credits=30,
        ),
        "image_to_video": ToolSpec(
            id="image_to_video",
            version="1.4.0",
            capability="video_generation",
            output_type="video",
            input_types={"image": ["image"]},
            params_schema={
                "duration_seconds": ParamRule(type="number", minimum=1, maximum=10, required=True)
            },
            ambiguous_without=["duration_seconds"],
            cost_credits=120,
        ),
        "cutout": ToolSpec(
            id="cutout",
            version="2.0.0",
            capability="matting",
            output_type="image",
            input_types={"image": ["image"]},
            params_schema={},
            cost_credits=5,
        ),
    }
    tools.update(overrides)
    return tools


def context(**overrides):
    data = {
        "user_id": "u1",
        "team_user_ids": ["u2"],
        "user_credits": 10_000,
        "run_budget": 5_000,
        "scope": {
            "tools": ["text_to_image", "image_edit", "image_to_video", "cutout"],
            "readable_assets": ["a_own", "a_team", "a_video"],
            "writable_regions": ["node_a", "node_b"],
        },
        "version_lock": {"tools": {name: spec.model_dump() for name, spec in catalog().items()}},
        "invariants": {},
        "canvas": {
            "nodes": [{"id": "node_a"}, {"id": "node_b"}],
            "assets": [
                {
                    "asset_id": "a_own",
                    "owner_id": "u1",
                    "kind": "image",
                    "width": 1080,
                    "height": 1920,
                },
                {"asset_id": "a_team", "owner_id": "u2", "kind": "image"},
                {"asset_id": "a_other", "owner_id": "u9", "kind": "image"},
                {
                    "asset_id": "a_video",
                    "owner_id": "u1",
                    "kind": "video",
                    "width": 1080,
                    "height": 1920,
                    "duration_seconds": 6,
                },
            ],
        },
    }
    data.update(overrides)
    return RunContext.model_validate(data)


def review(plan, ctx=None, **kwargs):
    return validate(plan, ctx or context(), **kwargs)


def codes(result: ReviewResult):
    return [reason.code for reason in result.reasons]


def image_step(step_id, **overrides):
    step = {
        "id": step_id,
        "tool": "text_to_image",
        "params": {"prompt": "一只猫", "aspect_ratio": "9:16"},
        "region": "node_a",
    }
    step.update(overrides)
    return step


def video_chain(tail_id="t2", duration=8, aspect="9:16", **tail_overrides):
    tail = {
        "id": tail_id,
        "tool": "image_to_video",
        "params": {"duration_seconds": duration},
        "inputs": {"image": "$t1.output"},
        "depends_on": ["t1"],
        "region": "node_b",
    }
    tail.update(tail_overrides)
    if aspect is None:
        tail["params"].pop("aspect_ratio", None)
    return [image_step("t1"), tail]


# --- gate ①: ticket legality --------------------------------------------------------------


def test_tool_must_exist_in_locked_version():
    assert "tool_not_locked" in codes(
        review({"steps": [{"id": "t1", "tool": "magic_wand", "params": {}}]})
    )
    assert review({"steps": [image_step("t1")]}).verdict == "pass"


def test_tool_disabled_in_lock_is_rejected():
    tools = catalog()
    tools["text_to_image"] = tools["text_to_image"].model_copy(update={"enabled": False})
    ctx = context(version_lock={"tools": {k: v.model_dump() for k, v in tools.items()}})
    result = validate({"steps": [image_step("t1")]}, ctx)
    assert "tool_disabled" in codes(result)


def test_tool_version_must_match_lock():
    result = review({"steps": [image_step("t1", tool_version="9.9.9")]})
    assert "tool_version_mismatch" in codes(result)
    assert review({"steps": [image_step("t1", tool_version="1.2.0")]}).verdict == "pass"


def test_tool_must_be_in_run_scope():
    ctx = context(scope={"tools": ["text_to_image"], "writable_regions": ["node_a", "node_b"]})
    result = validate(
        {
            "steps": [
                image_step("t1"),
                {
                    "id": "t2",
                    "tool": "cutout",
                    "inputs": {"image": "$t1.output"},
                    "region": "node_b",
                },
            ]
        },
        ctx,
    )
    assert "tool_out_of_scope" in codes(result)
    assert validate({"steps": [image_step("t1")]}, ctx).verdict == "pass"


def test_unknown_parameter_is_rejected():
    result = review({"steps": [image_step("t1", params={"prompt": "猫", "wizardry": 1})]})
    assert "param_unknown" in codes(result)


@pytest.mark.parametrize(
    "params",
    [
        {"prompt": 123, "aspect_ratio": "9:16"},
        {"prompt": "猫", "count": "three"},
    ],
)
def test_parameter_type_is_checked(params):
    assert "param_type_invalid" in codes(review({"steps": [image_step("t1", params=params)]}))
    assert review({"steps": [image_step("t1")]}).verdict == "pass"


def test_parameter_enum_is_checked():
    assert "param_enum_invalid" in codes(
        review({"steps": [image_step("t1", params={"prompt": "猫", "aspect_ratio": "4:3"})]})
    )
    assert review({"steps": [image_step("t1")]}).verdict == "pass"


def test_video_duration_above_maximum_is_rejected():
    result = review({"steps": video_chain(duration=30)})
    assert result.verdict == "reject"
    message = next(r.message for r in result.reasons if r.code == "param_above_maximum")
    assert message == "t2: image_to_video 的时长 30 秒超出上限 10 秒"


def test_parameter_below_minimum_is_rejected():
    assert "param_below_minimum" in codes(
        review({"steps": [image_step("t1", params={"prompt": "猫", "count": 0})]})
    )
    assert (
        review({"steps": [image_step("t1", params={"prompt": "猫", "count": 2})]}).verdict == "pass"
    )


def test_required_parameter_missing_is_rejected():
    assert "param_required_missing" in codes(
        review({"steps": [image_step("t1", params={"aspect_ratio": "9:16"})]})
    )


def test_referenced_asset_must_exist_on_canvas():
    result = review(
        {"steps": [{"id": "t1", "tool": "cutout", "inputs": {"image": {"asset_id": "a_ghost"}}}]}
    )
    assert "asset_unknown" in codes(result)


def test_referencing_someone_elses_asset_is_rejected():
    result = review(
        {"steps": [{"id": "t1", "tool": "cutout", "inputs": {"image": {"asset_id": "a_other"}}}]}
    )
    assert result.verdict == "reject"
    reason = next(r for r in result.reasons if r.code == "asset_not_owned")
    assert "a_other" in reason.message and "u9" in reason.message
    assert (
        review(
            {"steps": [{"id": "t1", "tool": "cutout", "inputs": {"image": {"asset_id": "a_own"}}}]}
        ).verdict
        == "pass"
    )
    assert (
        review(
            {"steps": [{"id": "t1", "tool": "cutout", "inputs": {"image": {"asset_id": "a_team"}}}]}
        ).verdict
        == "pass"
    )


def test_region_must_be_in_scope():
    assert "region_out_of_scope" in codes(review({"steps": [image_step("t1", region="node_x")]}))
    assert review({"steps": [image_step("t1", region="node_b")]}).verdict == "pass"


def test_cost_must_respect_run_budget():
    ctx = context(run_budget=10)
    result = validate({"steps": [image_step("t1")]}, ctx)
    assert "run_budget_exceeded" in codes(result)
    assert validate({"steps": [image_step("t1")]}, context(run_budget=100)).verdict == "pass"


def test_cost_must_respect_user_credits():
    ctx = context(user_credits=15)
    result = validate({"steps": [image_step("t1")]}, ctx)
    assert "credits_exceeded" in codes(result)
    assert validate({"steps": [image_step("t1")]}, context(user_credits=25)).verdict == "pass"


def test_undecided_video_params_require_ask_user_first():
    result = review(
        {
            "steps": [
                {"id": "t1", "tool": "image_to_video", "inputs": {"image": {"asset_id": "a_own"}}}
            ]
        }
    )
    # Required params are never exempted: an unanswerable ticket must not be admitted.
    assert "param_required_missing" in codes(result)
    assert "video_params_uncertain" in codes(result)
    ask_only = review(
        {"steps": [{"id": "t1", "kind": "ask_user", "params": {"question": "要几秒？"}}]}
    )
    assert ask_only.verdict == "pass"
    still_missing = review(
        {
            "steps": [
                {"id": "t1", "kind": "ask_user", "params": {"question": "要几秒？"}},
                {
                    "id": "t2",
                    "tool": "image_to_video",
                    "depends_on": ["t1"],
                    "inputs": {"image": {"asset_id": "a_own"}},
                },
            ]
        }
    )
    assert "param_required_missing" in codes(still_missing)


def test_replan_must_not_downgrade_video_to_image():
    previous = {"steps": video_chain(tail_id="t2")}
    ctx = context(replan={"previous": previous, "blocked_task_ids": []})
    replaced = {"steps": [image_step("t2")]}
    result = validate(replaced, ctx)
    assert "video_degradation" in codes(result)
    reason = next(r for r in result.reasons if r.code == "video_degradation")
    assert reason.step == "t2"
    kept = {
        "steps": [
            image_step("t1"),
            {
                "id": "t2",
                "tool": "image_to_video",
                "params": {"duration_seconds": 8},
                "inputs": {"image": "$t1.output"},
                "depends_on": ["t1"],
            },
        ]
    }
    assert validate(kept, ctx).verdict == "pass"


def test_blocked_by_model_task_must_not_switch_model():
    previous = {
        "steps": [
            {
                "id": "t2",
                "tool": "image_edit",
                "params": {"prompt": "修图"},
                "inputs": {"image": {"asset_id": "a_own"}},
            }
        ]
    }
    ctx = context(replan={"previous": previous, "blocked_task_ids": ["t2"]})
    switched = {
        "steps": [
            {"id": "t2", "tool": "text_to_image", "params": {"prompt": "重画"}, "region": "node_a"}
        ]
    }
    assert "blocked_model_switch" in codes(validate(switched, ctx))
    retried = {
        "steps": [
            {
                "id": "t2",
                "tool": "image_edit",
                "params": {"prompt": "修图"},
                "inputs": {"image": {"asset_id": "a_own"}},
            }
        ]
    }
    assert validate(retried, ctx).verdict == "pass"


def test_duplicate_task_fingerprint_is_rejected():
    twin = {
        "id": "t3",
        "tool": "text_to_image",
        "params": {"prompt": "一只猫", "aspect_ratio": "9:16"},
        "region": "node_b",
    }
    result = review({"steps": [image_step("t1"), twin]})
    assert "duplicate_task" in codes(result)
    different = dict(twin, params={"prompt": "一只狗", "aspect_ratio": "9:16"})
    assert review({"steps": [image_step("t1"), different]}).verdict == "pass"


def test_ask_user_step_must_not_carry_a_tool():
    assert "ask_user_with_tool" in codes(
        review({"steps": [{"id": "t1", "kind": "ask_user", "tool": "text_to_image"}]})
    )


def test_empty_tool_scope_rejects_locked_tools():
    # Both whitelist fields share whitelist semantics: empty means nothing is allowed.
    ctx = context(scope={"tools": [], "writable_regions": ["node_a"]})
    result = validate({"steps": [image_step("t1")]}, ctx)
    assert "tool_out_of_scope" in codes(result)


def test_ask_user_inputs_are_validated():
    dangling = review(
        {
            "steps": [
                {"id": "t1", "kind": "ask_user", "inputs": {"image": "$t9.output"}},
                image_step("t2"),
            ]
        }
    )
    assert "missing_upstream" in codes(dangling)
    stolen = review(
        {"steps": [{"id": "t1", "kind": "ask_user", "inputs": {"image": {"asset_id": "a_other"}}}]}
    )
    assert "asset_not_owned" in codes(stolen)
    fine = review(
        {"steps": [{"id": "t1", "kind": "ask_user", "inputs": {"image": {"asset_id": "a_own"}}}]}
    )
    assert fine.verdict == "pass"


def test_step_ids_must_be_unique():
    result = review({"steps": [image_step("t1"), image_step("t1")]})
    assert "duplicate_step_id" in codes(result)


# --- gate ②: typed linkage ----------------------------------------------------------------


def test_upstream_reference_must_exist():
    result = review({"steps": [{"id": "t2", "tool": "cutout", "inputs": {"image": "$t9.output"}}]})
    assert "missing_upstream" in codes(result)
    assert (
        review(
            {
                "steps": [
                    image_step("t1"),
                    {
                        "id": "t2",
                        "tool": "cutout",
                        "inputs": {"image": "$t1.output"},
                        "depends_on": ["t1"],
                        "region": "node_b",
                    },
                ]
            }
        ).verdict
        == "pass"
    )


def test_input_parameter_must_be_declared_by_tool():
    result = review(
        {
            "steps": [
                image_step("t1"),
                {"id": "t2", "tool": "cutout", "inputs": {"soundtrack": "$t1.output"}},
            ]
        }
    )
    assert "undeclared_input" in codes(result)


def test_video_into_cutout_is_rejected():
    steps = [
        {
            "id": "t1",
            "tool": "image_to_video",
            "params": {"duration_seconds": 8},
            "inputs": {"image": {"asset_id": "a_own"}},
        },
        {"id": "t2", "tool": "cutout", "inputs": {"image": "$t1.output"}, "depends_on": ["t1"]},
    ]
    result = review({"steps": steps})
    assert result.verdict == "reject"
    reason = next(r for r in result.reasons if r.code == "input_type_mismatch")
    assert reason.step == "t2" and "video" in reason.message and "t1" in reason.message


def test_video_asset_into_cutout_is_rejected():
    result = review(
        {"steps": [{"id": "t1", "tool": "cutout", "inputs": {"image": {"asset_id": "a_video"}}}]}
    )
    assert "input_type_mismatch" in codes(result)


def test_dependency_must_exist():
    assert "missing_dependency" in codes(review({"steps": [image_step("t1", depends_on=["t7"])]}))


def test_dependency_cycle_is_rejected():
    result = review(
        {
            "steps": [
                {
                    "id": "t1",
                    "tool": "cutout",
                    "depends_on": ["t2"],
                    "inputs": {"image": {"asset_id": "a_own"}},
                },
                {
                    "id": "t2",
                    "tool": "cutout",
                    "depends_on": ["t1"],
                    "inputs": {"image": {"asset_id": "a_team"}},
                },
            ]
        }
    )
    assert "dependency_cycle" in codes(result)
    message = next(r.message for r in result.reasons if r.code == "dependency_cycle")
    assert "t1 → t2 → t1" in message


def test_linear_chain_passes():
    steps = [
        image_step("t1"),
        {
            "id": "t2",
            "tool": "image_edit",
            "params": {"prompt": "加帽子"},
            "inputs": {"image": "$t1.output"},
            "depends_on": ["t1"],
            "region": "node_b",
        },
        {
            "id": "t3",
            "tool": "cutout",
            "inputs": {"image": "$t2.output"},
            "depends_on": ["t2"],
            "region": "node_a",
        },
    ]
    assert review({"steps": steps}).verdict == "pass"


# --- gate ③: invariants over final deliverables -------------------------------------------


def portrait_invariants(**overrides):
    data = {"count": 3, "modality": "image", "aspect_ratio": "9:16"}
    data.update(overrides)
    return {"deliverable": data}


def test_user_wants_three_portrait_but_plan_makes_two_landscape():
    ctx = context(invariants=portrait_invariants())
    result = validate(
        {"steps": [image_step("t1", params={"prompt": "猫", "aspect_ratio": "16:9", "count": 2})]},
        ctx,
    )
    assert result.verdict == "reject"
    assert "deliverable_count" in codes(result)
    aspect = next(r for r in result.reasons if r.code == "aspect_mismatch")
    assert aspect.step == "t1" and "16:9" in aspect.message and "9:16" in aspect.message


def test_matching_three_portrait_plan_passes():
    ctx = context(invariants=portrait_invariants())
    result = validate(
        {"steps": [image_step("t1", params={"prompt": "猫", "aspect_ratio": "9:16", "count": 3})]},
        ctx,
    )
    assert result.verdict == "pass"


def test_modality_mismatch_is_rejected():
    ctx = context(invariants=portrait_invariants(count=1, modality="video"))
    result = validate(
        {"steps": [image_step("t1", params={"prompt": "猫", "aspect_ratio": "9:16", "count": 1})]},
        ctx,
    )
    assert "modality_mismatch" in codes(result)
    video_ok = context(
        invariants={"deliverable": {"count": 1, "modality": "video", "duration_seconds": 8}}
    )
    assert validate({"steps": video_chain()}, video_ok).verdict == "pass"


def test_duration_invariant_mismatch_is_rejected():
    ctx = context(invariants={"deliverable": {"duration_seconds": 10}})
    result = validate({"steps": video_chain(duration=5)}, ctx)
    assert "duration_mismatch" in codes(result)
    exact = context(invariants={"deliverable": {"duration_seconds": 8}})
    assert validate({"steps": video_chain(duration=8)}, exact).verdict == "pass"


def test_required_asset_must_reach_final_deliverables():
    ctx = context(invariants={"deliverable": {"required_asset_ids": ["a_own"]}})
    missing = validate({"steps": [image_step("t1")]}, ctx)
    assert "required_asset_missing" in codes(missing)
    used = validate(
        {
            "steps": [
                {
                    "id": "t1",
                    "tool": "image_edit",
                    "params": {"prompt": "参考它"},
                    "inputs": {"image": {"asset_id": "a_own"}},
                }
            ]
        },
        ctx,
    )
    assert used.verdict == "pass"


def test_aspect_is_derived_through_input_chain():
    # The cutout itself carries no aspect parameter; it must inherit 9:16 from the source asset.
    ctx = context(invariants=portrait_invariants(count=1))
    result = validate(
        {"steps": [{"id": "t1", "tool": "cutout", "inputs": {"image": {"asset_id": "a_own"}}}]}, ctx
    )
    assert result.verdict == "pass"


def test_size_invariant_is_checked_when_ratio_absent():
    ctx = context(invariants={"deliverable": {"width": 1080, "height": 1920}})
    result = validate(
        {
            "steps": [
                image_step(
                    "t1",
                    params={
                        "prompt": "猫",
                        "aspect_ratio": "9:16",
                        "count": 1,
                        "width": 720,
                        "height": 1280,
                    },
                )
            ]
        },
        ctx,
    )
    assert "size_mismatch" in codes(result)


def test_invariant_aspect_ratio_is_normalized_before_compare():
    # 18:32 and 9:16 are the same ratio; a raw-text compare would falsely reject.
    ctx = context(
        invariants={"deliverable": {"count": 1, "modality": "image", "aspect_ratio": "18:32"}}
    )
    result = validate(
        {"steps": [image_step("t1", params={"prompt": "猫", "aspect_ratio": "9:16", "count": 1})]},
        ctx,
    )
    assert result.verdict == "pass"


def test_integral_floats_count_as_numbers():
    # JSON numbers like 2.0 pass the schema; deliverable derivation must read them as 2.
    ctx = context(invariants={"deliverable": {"count": 2}})
    result = validate(
        {
            "steps": [
                image_step("t1", params={"prompt": "猫", "aspect_ratio": "9:16", "count": 2.0})
            ]
        },
        ctx,
    )
    assert result.verdict == "pass"
    ctx_video = context(invariants={"deliverable": {"duration_seconds": 8}})
    assert validate({"steps": video_chain(duration=8.0)}, ctx_video).verdict == "pass"


def test_dependency_only_edge_still_excludes_upstream_from_deliverables():
    # Documents the definition: a step anything depends on (even without consuming its
    # output) is not a final deliverable — 无下游 means no dependents and no references.
    ctx = context(invariants={"deliverable": {"count": 2}})
    plan = {
        "steps": [
            image_step("t1", params={"prompt": "甲", "aspect_ratio": "9:16"}),
            {
                "id": "t2",
                "tool": "text_to_image",
                "params": {"prompt": "乙", "aspect_ratio": "9:16"},
                "depends_on": ["t1"],
                "region": "node_b",
            },
        ]
    }
    result = validate(plan, ctx)
    assert "deliverable_count" in codes(result)  # only t2 is a deliverable → 1 ≠ 2


# --- shared entry, parse failures, extension hook ------------------------------------------


def test_hand_edited_plan_dict_takes_the_same_entry():
    plan = {"steps": [image_step("t1")]}
    assert validate(plan, context()).verdict == "pass"
    broken = {"steps": [image_step("t1", params={"prompt": "猫", "count": 9})]}
    assert "param_above_maximum" in codes(validate(broken, context()))


def test_malformed_plan_is_rejected_with_location():
    result = review({"steps": [{"id": "s1", "tool": "text_to_image"}]})
    assert result.verdict == "reject"
    assert "invalid_plan" in codes(result)
    assert review({"steps": []}).verdict == "reject"


def test_intent_extension_rules_plug_into_gate_three():
    def must_mention_cats(view):
        prompts = [s.params.get("prompt", "") for s in view.plan.steps]
        if not any("猫" in prompt for prompt in prompts):
            return [intent_reason("no_cats", "计划没有提到猫", step=view.plan.steps[0].id)]
        return []

    plan = {"steps": [image_step("t1", params={"prompt": "一只狗", "aspect_ratio": "9:16"})]}
    assert review(plan, intent_extensions=[must_mention_cats]).verdict == "reject"
    cats = {"steps": [image_step("t1")]}
    assert review(cats, intent_extensions=[must_mention_cats]).verdict == "pass"


def test_estimated_cost_uses_override_then_tool_price():
    result = review({"steps": [image_step("t1", estimated_cost=7)]})
    assert result.estimated_cost == 7
    assert review({"steps": [image_step("t1")]}).estimated_cost == 20


def test_twenty_step_plan_reviews_under_20ms():
    steps = [image_step("t1")]
    for index in range(2, 21):
        tool = "image_edit" if index % 2 == 0 else "cutout"
        step = {
            "id": f"t{index}",
            "tool": tool,
            "inputs": {"image": f"$t{index - 1}.output"},
            "depends_on": [f"t{index - 1}"],
            "region": "node_a",
        }
        if tool == "image_edit":
            step["params"] = {"prompt": f"第 {index} 步微调"}
        steps.append(step)
    plan = {"steps": steps}
    assert len(steps) == 20
    ctx = context()  # assert pass so timing covers the full rule set
    assert validate(plan, ctx).verdict == "pass"
    started = time.perf_counter()
    result = validate(plan, ctx)
    elapsed = time.perf_counter() - started
    assert result.verdict == "pass"
    assert elapsed < 0.020, f"20 步计划审核耗时 {elapsed * 1000:.2f}ms，超过 20ms 上限"
