"""All writers use the same pure programmatic gates; no generation API is involved."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.agent.admission.policy import IntentContract, Proposal, ReviewSettings, digest, review
from backend.agent.media.planner import plan_intent
from backend.agent.media.catalog import Catalog
from test_host_media_contracts import config


def setup():
    catalog = Catalog(config())
    plan = plan_intent("生成四方向图片", [], catalog).model_dump(mode="json")
    intent = IntentContract(message=plan["goal"], expectedPlan=deepcopy(plan), readableAssets=[],
        writableNodes=[node for step in plan["steps"] for node in step["targetNodes"]])
    return catalog, plan, intent


@pytest.mark.parametrize("writer", ["rule", "small", "llm", "skill", "helper", "user"])
def test_every_writer_has_identical_checks(writer):
    catalog, plan, intent = setup()
    good = review(Proposal(writer=writer, plan=plan), intent, catalog, ReviewSettings())
    assert good.verdict == "approved" and all(good.checks.values()) and good.quote == 40
    plan["steps"][0]["fanOut"] = 1
    assert review(Proposal(writer=writer, plan=plan), intent, catalog, ReviewSettings()).verdict == "rejected"


@pytest.mark.parametrize("mutation,code", [
    (lambda p: p.update(providerSnapshot={}), "invalid_plan"),
    (lambda p: p["steps"][0].update(dependsOn=["missing"]), "plan_bad_dependency"),
    (lambda p: p["steps"][0].update(inputFrom=[{"stepId": "directions"}]), "plan_input_not_dependent"),
    (lambda p: p["steps"][0]["targetNodes"].__setitem__(0, "foreign"), "node_out_of_scope"),
    (lambda p: p.update(sourceAssetIds=["foreign"]), "asset_out_of_scope"),
    (lambda p: p["steps"][0].update(prompt="另一个人的广告"), "intent_contract_mismatch"),
    (lambda p: p["steps"][0].update(capabilityId="text_to_video"), "capability_unavailable"),
])
def test_illegal_or_off_target_proposals_never_pass(mutation, code):
    catalog, plan, intent = setup()
    mutation(plan)
    result = review(Proposal(writer="user", plan=plan), intent, catalog, ReviewSettings())
    assert result.verdict == "rejected"
    assert code in {issue.code for issue in result.issues}


def test_configured_expansion_and_credit_limits():
    catalog, plan, intent = setup()
    for settings, code in [(ReviewSettings(maxExpandedTasks=3), "plan_limit"),
                           (ReviewSettings(maxCredits=39), "credit_budget_exceeded")]:
        result = review(Proposal(writer="rule", plan=plan), intent, catalog, settings)
        assert code in {issue.code for issue in result.issues}


def test_video_ambiguity_and_content_block_are_hard_rules():
    catalog, plan, intent = setup()
    proposal = Proposal(writer="rule", plan=plan)
    for message, code in [("生成视频", "video_degradation"), ("动起来", "intent_requires_clarification")]:
        result = review(proposal, intent.model_copy(update={"message": message}), catalog, ReviewSettings())
        assert result.verdict != "approved"
        assert code in {issue.code for issue in result.issues}
    blocked = review(proposal, intent.model_copy(update={"blocked": True}), catalog, ReviewSettings())
    assert blocked.verdict == "rejected" and blocked.issues[0].code == "content_blocked"
    unclear = review(proposal, intent.model_copy(update={"ambiguous": True}), catalog, ReviewSettings())
    assert unclear.verdict == "clarify"


def test_copy_bypasses_and_coordinates_cannot_enter_contract():
    catalog, plan, intent = setup()
    plan["steps"][0]["x"] = 12
    assert review(Proposal(writer="rule", plan=plan), intent, catalog, ReviewSettings()).verdict == "rejected"
    with pytest.raises(ValidationError):
        ReviewSettings(maxTasks=0)
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_persisted_model_receipt_must_contain_exact_plan():
    import json
    from backend.agent.admission.service import model_contains_plan
    _, plan, _ = setup()
    assert model_contains_plan({"content": json.dumps({"plan": plan})}, plan)
    assert model_contains_plan({"tool_calls": [{"function": {"arguments": json.dumps(plan)}}]}, plan)
    assert not model_contains_plan({"content": "not json"}, plan)
    assert not model_contains_plan({"content": json.dumps({"goal": "other"})}, plan)


def test_existing_evaluation_flow_executes_real_admission_checks(tmp_path):
    from types import SimpleNamespace
    from backend.evaluation.agent_flow import execute_flow_case, observe_agent_flow, checks_for_expect
    catalog, plan, intent = setup()
    expect = {"admissionVerdict": "approved", "admissionLegal": True,
        "admissionTypes": True, "admissionIntent": True}
    case = SimpleNamespace(caseId="admission-unit", suites=["agent_flow"], input=SimpleNamespace(params={
        "expect": expect, "admission": {"catalog": catalog.config.model_dump(mode="json"),
            "proposal": {"writer": "rule", "plan": plan}, "intent": intent.model_dump(mode="json")}}))
    observation = execute_flow_case(tmp_path, case, tmp_path / "observation")
    answers, _ = observe_agent_flow(case, observation)
    from backend.evaluation.plan_admission import admission_check_id
    assert {key: answer.observed for key, answer in answers.items()} == {admission_check_id(k): v for k, v in expect.items()}
    assert {check["id"] for check in checks_for_expect(expect)} == {admission_check_id(k) for k in expect}
    assert (tmp_path / "observation" / "observation.json").is_file()


def test_animation_style_is_image_and_confirmed_motion_can_proceed():
    from backend.agent.media.intent.types import IntentDecision
    from backend.agent.media.planner import plan_from_decision
    catalog, _, _ = setup()
    message = "生成动画风格海报"
    plan = plan_from_decision(IntentDecision(certainty="strong", l2="text_to_image", slots={"prompt": message}), [], catalog)
    intent = IntentContract(message=message, expectedPlan=plan.model_dump(mode="json"), readableAssets=[],
        writableNodes=plan.steps[0].targetNodes)
    assert review(Proposal(writer="rule", plan=plan.model_dump(mode="json")), intent, catalog, ReviewSettings()).verdict == "approved"
    ambiguous = intent.model_copy(update={"message": "动起来"})
    assert review(Proposal(writer="rule", plan=plan.model_dump(mode="json")), ambiguous, catalog, ReviewSettings()).verdict != "approved"


def test_video_output_cannot_feed_image_reference_input():
    from backend.agent.media.contracts import CatalogConfig
    raw = config().model_dump(mode="json")
    image = deepcopy(raw["capabilities"][0])
    edit = {**deepcopy(image), "id": "image_edit", "maxRefImages": 1}
    video = {**deepcopy(image), "id": "text_to_video", "outputType": "video"}
    video["binding"]["providerSnapshot"]["videoModel"] = "mock-video"
    catalog = Catalog(CatalogConfig.model_validate({**raw, "capabilities": [image, edit, video]}))
    plan = {"goal": "图片", "goalType": "image", "steps": [
        {"id": "v", "title": "视频", "capabilityId": "text_to_video", "dependsOn": [], "targetNodes": ["a"], "tier": "preview", "prompt": "test"},
        {"id": "i", "title": "图片", "capabilityId": "image_edit", "dependsOn": ["v"], "targetNodes": ["b"], "tier": "preview", "prompt": "test", "inputFrom": [{"stepId": "v"}]}]}
    intent = IntentContract(message="图片", expectedPlan=deepcopy(plan), readableAssets=[], writableNodes=["a", "b"])
    result = review(Proposal(writer="rule", plan=plan), intent, catalog, ReviewSettings())
    assert result.verdict == "rejected" and result.checks["types"] is False


def test_admission_dataset_imports_and_runs_in_existing_evaluation_backend(tmp_path):
    from pathlib import Path
    from backend.evaluation.datasets import read_jsonl
    from backend.evaluation.models import CaseVersion
    from backend.evaluation.agent_flow import execute_flow_case, observe_agent_flow
    source = Path("evals/datasets/agent-admission.jsonl")
    for raw in read_jsonl(source):
        case = CaseVersion.model_validate(raw)
        observation = execute_flow_case(Path.cwd(), case, tmp_path / case.caseId)
        answers, _ = observe_agent_flow(case, observation)
        from backend.evaluation.plan_admission import admission_check_id
        assert {key: answer.observed for key, answer in answers.items()} == {
            admission_check_id(k): v for k, v in case.input.params["expect"].items()}
