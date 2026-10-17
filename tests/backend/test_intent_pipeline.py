"""E/R/S 门禁：固定向量夹具隔离侧车；不将待审文档当作金牌。"""
import asyncio
import copy
import json
import uuid
from dataclasses import replace

import pytest

from backend.agent.media.catalog import Catalog, HostError
from backend.agent.media.contracts import CatalogConfig
from backend.agent.media.intent import IntentPipeline, IntentSettings
from backend.agent.media.intent.embedder import EmbedError, HTTPEmbedder
from backend.agent.media.intent.feasibility import build_feasibility
from backend.agent.media.intent.retriever import Retriever, load_library, library_digest
from backend.agent.media.intent.types import IntentDecision, Step
from backend.agent.media.planner import plan_from_decision, plan_from_multi, validate_plan


def catalog(video=True):
    capabilities = []
    for name, kind, refs in [('text_to_image', 'image', 0), ('image_edit', 'image', 1), ('text_to_video', 'video', 0), ('frames_to_video', 'video', 4)]:
        capabilities.append({'id': name, 'enabled': video or kind != 'video', 'outputType': kind,
            'maxRefImages': refs, 'tiers': {'preview': {'credits': 1, 'durationSec': 5, 'width': 128, 'height': 128, 'fps': 4}},
            'binding': {'version': 'test-' + uuid.uuid4().hex, 'engine': 'frames' if name == 'frames_to_video' else 'provider',
                'providerSnapshot': {} if name == 'frames_to_video' else {'provider': 'mock',
                    'configVersion': 'test', 'cloudModel': 'mock-image', 'videoModel': 'mock-video'}}})
    return Catalog(CatalogConfig.model_validate({'version': 'test', 'environment': 'development',
        'enabled': True, 'capabilities': capabilities}))


class BrokenEmbedder:
    async def embed(self, texts):
        raise EmbedError('超时')


class FixedEmbedder:
    model_id = 'test-v1'

    async def embed(self, texts):
        return [[1., 0.] for text in texts]


def pipeline(*, multi=False, video=True, l2='image_edit', broken=False):
    library = {'exemplars_version': '0.0.1', 'embed_model_id': 'test-v1', 'items': [
        {'id': 'approved-test', 'text': '仅用于单测的标注例句', 'l2': l2, 'approval': 'approved', 'approved_by': 'test-fixture'}]}
    return IntentPipeline(catalog(video), IntentSettings(enabled=True, multi_step=multi, model_id='test-v1'),
        retriever=Retriever(library, [[1., 0.]]), embedder=BrokenEmbedder() if broken else FixedEmbedder())


SNAPSHOT = {'document_id': 'doc', 'nodes': {'cat': {'kind': 'image', 'assetId': 'asset', 'assetVersion': 1}}}


def decide(pipe, text, refs=None):
    return asyncio.run(pipe.run(text, ['asset'] if refs is None else refs, request_id='original', snapshot=SNAPSHOT))


@pytest.mark.parametrize('text', ['活一点', '动起来', 'animate this image', '让人物稍微动一动'])
def test_e01_e04_weak_motion_never_silently_edits(text):
    assert decide(pipeline(), text).clarify


@pytest.mark.parametrize('text', ['动画风格海报', '别做成视频，换成水彩', 'anime style portrait poster'])
def test_e02_e03_style_and_negation_stay_image(text):
    value = decide(pipeline(), text)
    assert value.l2 == 'image_edit'
    assert plan_from_decision(value, ['asset'], catalog()).goalType == 'image'


def test_e05_embedding_failure_observable_and_e07_slots_cannot_rewrite_video():
    value = decide(pipeline(broken=True), '活一点')
    assert value.clarify and value.trace['embed_degraded']
    video = decide(pipeline(), '改成水彩做成视频')
    assert video.l2 == 'image_to_multiframe' and video.slots['style'] == 'watercolor'
    assert len(video.steps) == 1
    assert plan_from_decision(video, ['asset'], catalog()).goalType == 'video'


def test_e06_video_disabled_does_not_remove_intent_or_degrade():
    pipe = pipeline(video=False)
    value = decide(pipe, '做成视频')
    assert 'image_to_multiframe' in value.trace['possibleL2']
    assert not value.trace['executable']['capabilityOk']['image_to_multiframe']
    with pytest.raises(HostError) as exc:
        plan_from_decision(value, ['asset'], pipe.catalog)
    assert exc.value.code == 'capability_unavailable'


def test_e08_structure_filters_edit_and_does_not_call_tool_without_image():
    feas = build_feasibility(False, catalog())
    assert 'image_edit' not in feas.possibleL2 and 'image_to_multiframe' not in feas.possibleL2
    assert decide(pipeline(), '改成水彩', []).clarify


def test_e09_e10_resume_uses_original_snapshot_and_never_retrieves():
    pipe = pipeline()
    pending = decide(pipe, '活一点')
    pipe.embedder = BrokenEmbedder()
    click = pipe.resume(pending.clarify, '做成视频', snapshot=SNAPSHOT, option_id='image_to_multiframe', trace=pending.trace)
    assert click.l2 == 'image_to_multiframe' and click.trace['topK'] == []
    typed = pipe.resume(pending.clarify, '改图', snapshot=SNAPSHOT, trace=pending.trace)
    assert typed.l2 == 'image_edit' and typed.slots['prompt'] == '活一点'
    assert pipe.resume(pending.clarify, '随便吧', snapshot=SNAPSHOT).clarify
    changed = copy.deepcopy(SNAPSHOT)
    changed['nodes']['cat']['assetVersion'] = 2
    with pytest.raises(HostError, match='已变化'):
        pipe.resume(pending.clarify, '改图', snapshot=changed)
    with pytest.raises(HostError, match='过期'):
        pipe.resume(pending.clarify.model_copy(update={'expires_at': 0}), '改图', snapshot=SNAPSHOT)


def test_o01_trace_fields_present_without_raw_prompt():
    trace = decide(pipeline(), '做成视频').trace
    assert {'request_id', 'message_hash', 'possibleL2', 'topK', 'certainty', 'decided_l2', 'path',
        'embed_model_id', 'embed_degraded', 'rule_version', 'exemplars_version', 'gates_hit', 'steps', 'step_results'} <= trace.keys()
    assert '做成视频' not in json.dumps(trace, ensure_ascii=False)


@pytest.mark.parametrize('multi', [False, True])
def test_s01_s02_matting_sequence_flag_and_chained_refs(multi):
    value = decide(pipeline(multi=multi, broken=True), '先抠图去背景，再做成视频')
    if not multi:
        assert value.clarify and not value.steps
        assert not any(option.id == 'sequence_all' for option in value.clarify.options)
        return
    assert [step.l2 for step in value.steps] == ['image_edit', 'image_to_multiframe']
    assert value.steps[0].slots['op'] == 'matting'
    plan = plan_from_multi(value, ['asset'], catalog())
    assert not validate_plan(plan, catalog())
    assert plan.steps[0].capabilityId == 'image_edit'
    roots = [step for step in plan.steps if step.intentStepId == 's2' and step.capabilityId == 'image_edit']
    assert len(roots) == 4
    assert all(step.inputFrom[0].stepId == plan.steps[0].id for step in roots)
    assert all(step.dependsOn == [plan.steps[0].id] for step in roots)
    assert plan.sourceAssetIds == ['asset']


def test_s03_creation_without_image_then_video():
    value = decide(pipeline(multi=True, broken=True), '先画一只猫，再生成视频', [])
    assert [step.l2 for step in value.steps] == ['text_to_image', 'image_to_multiframe']
    plan = plan_from_multi(value, [], catalog())
    assert plan.steps[0].capabilityId == 'text_to_image' and not plan.sourceAssetIds


@pytest.mark.parametrize('text', ['先做成视频再改水彩', '先抠图，再水彩，再视频', '先视频后改图再视频'])
def test_s04_s05_reject_nonwhitelist_and_three_steps(text):
    assert decide(pipeline(multi=True), text).clarify


def test_s06_disabled_second_step_fails_before_admission():
    pipe = pipeline(multi=True, video=False, broken=True)
    value = decide(pipe, '先抠图再做成视频')
    with pytest.raises(HostError) as exc:
        plan_from_multi(value, ['asset'], pipe.catalog)
    assert exc.value.code == 'capability_unavailable'


def test_s08_style_video_no_sequence_and_same_l2_constraints_single_step():
    pipe = pipeline(multi=True)
    for text in ['改成水彩做成视频', '把衣服换成红的，整体水彩风', '把左下角 logo 去掉，颜色再鲜艳一点']:
        value = decide(pipe, text)
        assert not value.clarify and len(value.steps) == 1


def test_weak_sequence_needs_confirmation_and_flags_rechecked():
    pipe = pipeline(multi=True)
    value = decide(pipe, '一键：抠图+做成视频')
    assert value.clarify
    confirmed = pipe.resume(value.clarify, '一次做完', snapshot=SNAPSHOT, trace=value.trace)
    assert len(confirmed.steps) == 2
    pipe.settings = replace(pipe.settings, multi_step=False)
    with pytest.raises(HostError):
        pipe.resume(value.clarify, '一次做完', snapshot=SNAPSHOT)


def test_type_rejects_reverse_order_or_missing_dependency():
    with pytest.raises(ValueError):
        IntentDecision(certainty='strong', steps=[Step(id='s1', l2='text_to_video'),
            Step(id='s2', l2='image_edit', input_from='step:s1', depends_on=['s1'])])


def test_pending_exemplars_not_retrievable_and_versions_fail_closed(tmp_path):
    seed = load_library()
    assert seed['exemplars_version'] == '0.2.0'
    assert not Retriever(seed, []).items
    library = {'exemplars_version': '0.0.1', 'items': []}
    library_file, index = tmp_path / 'library.yaml', tmp_path / 'index.json'
    library_file.write_text(json.dumps(library))
    index.write_text(json.dumps({'library_digest': library_digest(library), 'embed_model_id': 'old',
        'exemplars_version': '0.0.1', 'vectors': []}))
    with pytest.raises(EmbedError):
        Retriever.from_cache(library_file, index, 'new')


@pytest.mark.parametrize('url', ['https://api.openai.com/v1/embeddings', 'https://example.com/', 'http://user:password@localhost/'])
def test_embedding_cannot_default_to_cloud(url):
    with pytest.raises(EmbedError):
        HTTPEmbedder(url, 'test-v1')


def test_many_same_class_exemplars_cannot_hide_close_competing_class():
    from backend.agent.media.intent.decide import decide_from_hits
    library = {'exemplars_version': '0.0.1', 'items': [
        {'id': str(i), 'text': f'标注夹具{i}', 'l2': 'image_edit' if i < 10 else 'image_to_multiframe',
            'approval': 'approved', 'approved_by': 'fixture'} for i in range(11)]}
    retriever = Retriever(library, [[1., 0.]] * 10 + [[0.999, 0.001]])
    hits = retriever.search([1., 0.], ['image_edit', 'image_to_multiframe'])
    assert len(hits) == 2 and decide_from_hits(hits)[1] == 'weak'


def test_o02_o04_fixed_safety_shadow_has_baseline_denominator_and_no_regression(monkeypatch):
    from scripts.eval_intent import evaluate
    for name in ['INTENT_RAG', 'INTENT_MULTI_STEP', 'INTENT_INDEX', 'INTENT_EXEMPLARS', 'INTENT_EMBED_MODEL_ID']:
        monkeypatch.delenv(name, raising=False)
    report = asyncio.run(evaluate())
    assert all(row['passed'] for row in report['results'])
    assert report['high_cost_denominator'] > 0
    assert report['shadow_high_cost_errors'] <= report['baseline_high_cost_errors']


@pytest.mark.parametrize('response', [
    {'model': 'different', 'data': [{'embedding': [1., 0.]}]},
    {'model': 'test-v1', 'data': [{'embedding': [0., 0.]}]},
    {'model': 'test-v1', 'data': []},
])
def test_sidecar_model_or_vector_errors_fail_closed(monkeypatch, response):
    import httpx
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=transport, **kwargs))
    with pytest.raises(EmbedError):
        asyncio.run(HTTPEmbedder('http://localhost/v1/embeddings', 'test-v1').embed(['查询']))


def test_sidecar_uses_explicit_model_and_current_text_only(monkeypatch):
    import httpx
    real_client = httpx.AsyncClient
    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'model': 'test-v1', 'data': [{'embedding': [1., 0.]}]})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    embedder = HTTPEmbedder('http://localhost/v1/embeddings', 'test-v1')
    assert asyncio.run(embedder.embed(['当前原话\n[has_image=true]'])) == [[1., 0.]]
    assert captured == [{'model': 'test-v1', 'input': ['当前原话\n[has_image=true]']}]


def test_local_sidecar_protocol_and_wrong_model_rejection():
    from fastapi.testclient import TestClient
    from scripts.intent_embed_server import create_app
    class LocalModel:
        def encode(self, texts, normalize_embeddings):
            assert normalize_embeddings and texts == ['测试']
            import numpy as np
            return np.array([[1., 0.]])
    with TestClient(create_app(LocalModel(), 'local-fixed-v1')) as client:
        assert client.post('/v1/embeddings', json={'model': 'wrong', 'input': ['测试']}).status_code == 400
        response = client.post('/v1/embeddings', json={'model': 'local-fixed-v1', 'input': ['测试']})
        assert response.json() == {'model': 'local-fixed-v1', 'data': [{'embedding': [1., 0.]}]}
        assert client.post('/v1/embeddings', json={'model': 'local-fixed-v1', 'input': []}).status_code == 422


def test_single_step_slots_cannot_override_locked_l2_and_multiplan_uses_shared_contract():
    from backend.agent.media.contracts import MultiPlan, MediaPlan
    decision = IntentDecision(certainty='strong', l2='image_to_multiframe', path='image_to_multiframe',
        slots={'prompt': '视频', 'l2': 'image_edit', 'style': 'watercolor'})
    assert plan_from_decision(decision, ['asset'], catalog()).goalType == 'video'
    multi = plan_from_multi(decide(pipeline(multi=True, broken=True), '先抠图再做成视频'), ['asset'], catalog())
    assert isinstance(multi, MultiPlan)
    assert MediaPlan.model_validate(multi.model_dump()).goalType == 'video'


def test_sequential_weak_motion_only_runs_after_once_confirmation():
    pipe = pipeline(multi=True, broken=True)
    value = decide(pipe, '先抠图再动起来')
    assert value.clarify and not value.steps
    assert any(option.id == 'sequence_all' for option in value.clarify.options)
    assert len(pipe.resume(value.clarify, '一次做完', snapshot=SNAPSHOT, trace=value.trace).steps) == 2
    assert decide(pipe, '先抠图，然后再做成视频').steps
    assert decide(pipe, '先视频后改图再视频').clarify


def test_sidecar_total_request_budget_degrades_observably():
    class SlowEmbedder:
        async def embed(self, texts):
            await asyncio.sleep(1)
    pipe = pipeline()
    pipe.settings = replace(pipe.settings, timeout=0.01)
    pipe.embedder = SlowEmbedder()
    decision = decide(pipe, '把主体转换为水墨静态画面')
    assert decision.clarify and decision.trace['embed_degraded']


@pytest.mark.parametrize('message', ['生成4K图片', '生成8K图片'])
def test_new_template_rejects_unavailable_resolution_in_chinese_text(message):
    decision = IntentDecision(certainty='strong', l2='text_to_image', slots={'prompt': message})
    with pytest.raises(HostError) as exc:
        plan_from_decision(decision, [], catalog())
    assert exc.value.code == 'spec_not_allowed'


def test_explicit_missing_image_does_not_silently_become_text_video():
    decision = decide(pipeline(), '把这张图做成视频', [])
    with pytest.raises(HostError, match='参考图'):
        plan_from_decision(decision, [], catalog())


@pytest.mark.parametrize('message', [
    '不要输出视频，换成水彩', '不要生成任何视频，只改成水彩',
    '不需要制作短片，只要水彩图片', "don't output any video, change this to watercolor",
])
def test_video_negation_never_reaches_video_template(message):
    value = decide(pipeline(), message)
    assert value.l2 == 'image_edit'
    assert plan_from_decision(value, ['asset'], catalog()).goalType == 'image'


@pytest.mark.parametrize('message', ['不要动画风格，做成视频', "no animation style, make a video"])
def test_negated_art_style_does_not_negate_video_goal(message):
    value = decide(pipeline(), message)
    assert value.l2 == 'image_to_multiframe'


def test_conflicting_positive_and_negative_video_clauses_clarify():
    assert decide(pipeline(), '不要输出视频，但是做成视频').clarify


@pytest.mark.parametrize('message', [
    '不要抠图，只把衣服换成红色', '不用去背景，换成水彩',
    "don't remove the background, change the clothes to red",
])
def test_negated_matting_does_not_create_operation_slot(message):
    from backend.agent.media.intent.slots import fill_slots
    slots = fill_slots(message)
    assert 'op' not in slots and 'background' not in slots


def test_positive_matting_after_negative_other_clause_is_kept():
    from backend.agent.media.intent.slots import fill_slots
    assert fill_slots('不要换衣服，但去除背景')['op'] == 'matting'


def test_word_containing_bie_does_not_act_as_video_negation():
    value = decide(pipeline(), '分别生成视频')
    assert value.l2 == 'image_to_multiframe'
