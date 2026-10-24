"""Seed honesty, reference resolution, redo clone, and reference evaluation."""
import json
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator

from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.core.execution.adapters import StagedAdapter
from backend.core.session_redo import (
    RedoNotExecutable, SeedInvalid, SeedOutOfRange, SeedRange, SeedUnchanged, SeedUnsupported, clone_plan,
)
from backend.core.session_references import ArtifactCandidate, ReferenceQuery, ReferenceSettings, resolve
from backend.core.supervisor.contracts import Plan
from backend.core.tools import CatalogSnapshot, ToolCatalog, build_catalog
from backend.evaluation.reference_cases import case_rows, load_cases, write_fixture
from backend.evaluation.reference_dataset import DATASET_ID, write_reference_dataset
from backend.evaluation.reference_evaluation import score_case, summarize
from backend.evaluation.registry import all_evaluators, register_builtin
from backend.config import Config
from backend.models import GenParams
from backend.providers.cloud import CloudProvider
from backend.providers.seed_capability import provider_honors_seed
from backend.providers.workflows import image_workflow, ltx_workflow


def _catalog(provider, vendor="aliyun", output="image", capability="text_to_image"):
    raw = {"version": "unit", "environment": "development", "enabled": True, "capabilities": [{
        "id": capability, "enabled": True, "outputType": output,
        "tiers": {"preview": {"credits": 10, "width": 1280 if output == "video" else 512,
                              "height": 720 if output == "video" else 512, "durationSec": 5, "fps": 16}},
        "binding": {"version": "unit", "providerSnapshot": {
            "provider": provider, "configVersion": "unit", "comfyUrl": "http://127.0.0.1:8188", "cloudVendor": vendor,
            "cloudModel": "unit-model", "videoModel": "wanx2.1-t2v-turbo",
            "cloudBaseUrl": "https://provider.test/api/v1", "credentialRef": "test"}}}]}
    return build_catalog(Catalog(CatalogConfig.model_validate(raw)), "seed-test")


def test_seed_flag_matches_cloud_and_comfy_request_bodies():
    params = GenParams(prompt="synthetic", model="unit-model", width=512, height=512, seed=42)
    for vendor in ("aliyun", "siliconflow", "openai", "zhipu"):
        cloud = CloudProvider(Config(provider="cloud", cloudVendor=vendor, cloudBaseUrl="https://provider.test",
                                      cloudModel="unit-model", videoModel="wanx2.1-t2v-turbo"), None)
        _url, body = cloud.request(params, 42, None, None)
        encoded = json.dumps(body)
        assert (encoded.find("\"seed\": 42") >= 0) is provider_honors_seed("cloud", "image", vendor)
    video = GenParams(prompt="synthetic", model="unit-model", width=1280, height=720, seed=42, kind="video", durationSec=5)
    video_url, video_body = CloudProvider(Config(
        provider="cloud", cloudVendor="aliyun", cloudBaseUrl="https://provider.test/api/v1",
        cloudModel="unit-model", videoModel="wanx2.1-t2v-turbo"), None).video_request(video)
    assert "seed" not in json.dumps(video_body)
    assert video_url
    assert provider_honors_seed("cloud", "video", "aliyun") is False
    graph = image_workflow(params, 42)
    assert graph["3"]["inputs"]["seed"] == 42
    video = ltx_workflow(params, 42, "ltx.safetensors")
    assert any(node["inputs"].get("seed") == 42 or node["inputs"].get("noise_seed") == 42 for node in video.values())


def test_catalog_seed_schema_is_optional_and_old_snapshots_stay_unchanged():
    mock = _catalog("mock")
    tool = mock.get_tool("text_to_image_preview", "seed-test")
    assert tool.execution.binding["seed_supported"] is True
    assert "seed" in tool.parameters["properties"]
    assert "seed" not in tool.parameters["required"]
    assert _catalog("cloud", "siliconflow").get_tool("text_to_image_preview", "seed-test").execution.binding["seed_supported"] is False
    assert "seed" not in _catalog("cloud", "siliconflow").get_tool("text_to_image_preview", "seed-test").parameters["properties"]
    assert _catalog("cloud", "aliyun").get_tool("text_to_image_preview", "seed-test").execution.binding["seed_supported"] is True
    video = _catalog("cloud", "aliyun", "video", "text_to_video")
    assert video.get_tool("text_to_video_preview", "seed-test").execution.binding["seed_supported"] is False
    comfy = _catalog("comfyui", "aliyun", "video", "text_to_video")
    assert comfy.get_tool("text_to_video_preview", "seed-test").execution.binding["seed_supported"] is True
    raw = mock.snapshot.model_dump(mode="json")
    for item in raw["tools"]:
        item["parameters"]["properties"].pop("seed", None)
        item["execution"]["binding"].pop("seed_supported", None)
    old = ToolCatalog(CatalogSnapshot.model_validate(raw))
    assert "seed" not in old.get_tool("text_to_image_preview", "seed-test").parameters["properties"]
    assert old.lock.content_hash != mock.lock.content_hash
    assert old.snapshot.schema_version == 1
    published = old.lock
    before = old.snapshot.model_dump_json()
    fresh = _catalog("mock")
    fresh = ToolCatalog(fresh.snapshot.model_copy(update={"version": "published-v2"}))
    assert fresh.lock.version == "published-v2"
    assert fresh.lock != published
    assert old.snapshot.model_dump_json() == before
    assert old.lock == published
    assert "seed" not in old.get_tool("text_to_image_preview", "seed-test").parameters["properties"]
    Draft202012Validator(old.get_tool("text_to_image_preview", "seed-test").parameters).validate(
        {"prompt": "测试", "count": 1, "width": 512, "height": 512})


def test_adapter_forwards_seed_only_when_the_binding_allows_it(tmp_path):
    tool = _catalog("cloud", "aliyun").get_tool("text_to_image_preview", "seed-test")
    seen = {}

    def capture(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"output": {"choices": [{"message": {"content": [{"image": "https://provider.test/a.png"}]}}]}})

    params = {"prompt": "synthetic", "count": 1, "width": 512, "height": 512, "seed": 7, "_prepared": {}}
    import asyncio
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
            adapter = StagedAdapter(tool, client, tmp_path, {"test": {"imageApiKey": "fake"}}, set())
            assert adapter.arguments({"prompt": "synthetic", "width": 512, "height": 512, "seed": 7})[0].seed == 7
            await adapter.submit("registration", params, None)
        unsupported = _catalog("cloud", "openai").get_tool("text_to_image_preview", "seed-test")
        async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
            adapter = StagedAdapter(unsupported, client, tmp_path, {"test": {"imageApiKey": "fake"}}, set())
            with pytest.raises(ValueError, match="seed_unsupported"):
                adapter.arguments({"prompt": "synthetic", "width": 512, "height": 512, "seed": 7})
    asyncio.run(scenario())
    assert seen["body"]["parameters"]["seed"] == 7


def test_reference_resolution_and_redo_clone():
    items = [ArtifactCandidate.model_validate(row) for row in case_rows()[0]["artifacts"]]
    found = resolve(items, ReferenceQuery(caller_session_id="s1", kind="latest"))
    assert found.status == "resolved" and found.artifact_id == "p2"
    with pytest.raises(ValueError):
        ArtifactCandidate.model_validate({**case_rows()[0]["artifacts"][0], "x": float("nan")})
    with pytest.raises(ValueError):
        ArtifactCandidate.model_validate({**case_rows()[0]["artifacts"][0], "y": float("inf")})
    catalog = _catalog("mock").snapshot
    plan = Plan(goal="redo", control_audit={"tool": "note", "arguments": {"keep": {"v": 1}}}, steps=[
        {"id": "t1", "tool": "text_to_image_preview", "parameters": {"prompt": "rabbit", "nested": {"k": 1}, "seed": 3}},
        {"id": "t2", "tool": "cutout", "parameters": {"image": {"id": "asset", "meta": {"n": 1}}},
         "depends_on": ["t1"], "placement": {"anchor": {"id": "n"}}}])
    cloned = clone_plan(plan, catalog, 9, seed_range=SeedRange(minimum=1, maximum=20))
    assert cloned.steps[0].parameters["seed"] == 9
    assert cloned.steps[0].parameters["prompt"] == "rabbit"
    assert cloned.steps[0].parameters["nested"] == {"k": 1}
    assert "seed" not in cloned.steps[1].parameters
    assert cloned.steps[1].depends_on == ["t1"]
    assert cloned.control_audit.arguments["keep"]["v"] == 1
    plan.steps[1].parameters["image"]["meta"]["n"] = 99
    plan.control_audit.arguments["keep"]["v"] = 0
    cloned.steps[0].parameters["nested"]["k"] = 8
    assert plan.steps[0].parameters["nested"]["k"] == 1
    assert cloned.steps[1].parameters["image"]["meta"]["n"] == 1
    assert cloned.control_audit.arguments["keep"]["v"] == 1
    assert plan.control_audit.arguments["keep"]["v"] == 0
    with pytest.raises(SeedUnsupported):
        clone_plan(plan, _catalog("cloud", "openai").snapshot, 9)
    with pytest.raises(SeedOutOfRange):
        clone_plan(plan, catalog, 100, seed_range=SeedRange(minimum=0, maximum=10))
    with pytest.raises(SeedUnchanged):
        clone_plan(plan, catalog, 3, seed_range=SeedRange(minimum=1, maximum=20))
    with pytest.raises(SeedOutOfRange):
        clone_plan(plan, catalog, 3, seed_range=SeedRange(minimum=3, maximum=3))
    with pytest.raises(SeedInvalid):
        clone_plan(plan, catalog, True)
    deterministic = Plan(goal="cut", steps=[{"id": "t1", "tool": "cutout", "parameters": {"image": {"id": "a"}}}])
    copied = clone_plan(deterministic, catalog, 4)
    assert copied.steps[0].parameters == {"image": {"id": "a"}}
    with pytest.raises(SeedInvalid):
        clone_plan(deterministic, catalog, False)
    with pytest.raises(RedoNotExecutable):
        clone_plan(Plan(goal="talk", result_type="answer_only", answer="ok", steps=[]), catalog, 4)
    with pytest.raises(RedoNotExecutable):
        clone_plan(Plan(goal="ask", result_type="clarify", steps=[{"id": "t1", "kind": "ask_user", "parameters": {"q": "哪张"}}]), catalog, 4)
    tight = ReferenceSettings(candidate_limit=1)
    assert resolve(items, ReferenceQuery(caller_session_id="s1", kind="latest"), tight).reason == "candidate_limit"


def test_reference_cases_score_and_register(tmp_path: Path):
    fixture = write_fixture(tmp_path / "session-reference-cases.jsonl")
    cases = load_cases(fixture)
    assert len(cases) >= 36
    assert all(case.utterance for case in cases)
    scores = [score_case(case) for case in cases]
    summary = summarize(scores, cases)
    missed = [item.case_id for item in scores if not (item.status_match and item.artifact_match and item.reason_match)]
    assert missed == []
    assert summary.llm_tested is False
    assert summary.rule_pass == summary.case_count
    register_builtin()
    declared = {item.id for item in all_evaluators()}
    assert "typed-rules" in declared
    from backend.evaluation.runner import EvaluationRunner
    from backend.evaluation.reports import build_report
    source = write_reference_dataset(tmp_path / "cases.jsonl", fixture)
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")
    runner.freeze_dataset(source, DATASET_ID, split="regression")
    runner.create_run("ref-rule", DATASET_ID, mode="mock", purpose="reference rules")
    state = runner.execute_run("ref-rule")
    assert state.status == "completed"
    report = build_report(state, runner.datasets.load(DATASET_ID, state.manifest.dataset["version"]).cases)
    assert report["sessionReference"]["llmTested"] is False
    assert report["sessionReference"]["llm"] is None
    assert report["sessionReference"]["rule"]["status"]["rate"] == 1
    from backend.evaluation.reference_evaluation import grade_recorded
    from backend.evaluation.runner import EventLog
    trial = state.trials[0]
    path = runner._trial_dir(trial.runId, trial.trialId) / "observation.json"
    recorded = json.loads(path.read_text(encoding="utf-8"))
    assert "query" not in recorded
    recorded["observed_artifact_id"] = "mutated-not-expected"
    path.write_text(json.dumps(recorded), encoding="utf-8")
    versions = runner.datasets.load(DATASET_ID, state.manifest.dataset["version"]).cases
    case = next(item for item in versions if item.caseId == trial.caseId)
    runner._grade_reference(trial, case, EventLog(runner.run_dir(trial.runId)))
    assert trial.qualityVerdict == "fail"
    graded = grade_recorded(cases[0], {"observed_status": "resolved"})
    assert graded == "observation_missing_or_corrupt"
