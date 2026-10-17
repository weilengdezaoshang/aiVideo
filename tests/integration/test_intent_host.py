"""独立 PostgreSQL/Redis 中验证意图接入、持久化澄清及顺位执行。"""
import asyncio
import copy
import uuid

import pytest
from sqlalchemy import select

from backend.agent.media.catalog import HostError
from backend.agent.orm import AgentRun, HostCreditLedger
from backend.infrastructure.orm import Asset, Document, Job
from tests.integration.test_host_media import fixture, finish_jobs, DB, REDIS
from tests.backend.test_intent_pipeline import pipeline

pytestmark = pytest.mark.skipif(not DB or not REDIS, reason='需要显式独立测试数据库与 Redis')


def body(document, message, session=None, revision=1, selection=None, **extra):
    ident = str(uuid.uuid4())
    selection = selection or []
    return {'clientRequestId': ident, 'documentId': str(document), 'message': message,
        'baseRevision': revision, 'selectedObjectIds': selection,
        'context': {'schema_version': 1, 'request_id': ident, 'conversation_id': session or str(uuid.uuid4()),
            'doc_id': str(document), 'doc_version': revision, 'client_ts': 1,
            'client_id': 'test', 'tab_id': 'test', 'message_text': message,
            'selection': {'ids': selection, 'count': len(selection), 'truncated': False},
            'viewport': {'bbox': [0., 0., 100., 100.], 'zoom': 1.}}, **extra}


async def add_image(factory, workspace, document):
    ident = uuid.uuid4()
    async with factory.begin() as db:
        db.add(Asset(id=ident, workspace_id=workspace, kind='image', ext='png',
            storage_key=f'{ident}.png', sha256=ident.hex * 2, bytes=10, details={}))
        doc = await db.get(Document, document)
        doc.data = {'objects': {'cat': {'id': 'cat', 'kind': 'image', 'assetId': str(ident), 'assetVersion': 1}}, 'order': ['cat']}
    return str(ident)


def test_e09_a01_original_snapshot_click_is_durable_without_retrieval(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, _):
            service.intent = pipeline(broken=True)
            service.catalog = service.intent.catalog
            source = await add_image(factory, workspace, document)
            first = await service.create(workspace, 'owner', body(document, '活一点', selection=['cat']))
            assert first['run']['status'] == 'waiting_input'
            assert first['run']['waiting']['options']
            original = uuid.UUID(first['run']['id'])
            session = first['run']['sessionId']
            raw = body(document, '做成视频', session, clarifyRunId=str(original), clarifyOptionId='image_to_multiframe')
            accepted = await service.create(workspace, 'owner', raw)
            assert accepted['run']['media']['goalType'] == 'video'
            async with factory() as db:
                run = await db.get(AgentRun, uuid.UUID(accepted['run']['id']))
                assert run.execution['plan']['sourceAssetIds'] == [source]
                assert run.snapshot['selectedObjectIds'] == ['cat']
                feedback = run.execution['intentFeedback'][0]
                assert feedback['source'] == 'clarify_click' and feedback['utterance'] == '活一点'
                assert feedback['request_id'] == first['run']['clientRequestId']
                assert feedback['approval'] == 'pending'
            assert (await service.create(workspace, 'owner', raw))['replayed']
    asyncio.run(scenario())


def test_e10_unparsed_text_stays_pending_and_changed_original_is_rejected(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, _):
            service.intent = pipeline(broken=True)
            service.catalog = service.intent.catalog
            await add_image(factory, workspace, document)
            first = (await service.create(workspace, 'owner', body(document, '活一点', selection=['cat'])))['run']
            second = (await service.create(workspace, 'owner', body(document, '随便', first['sessionId'], clarifyRunId=first['id'])))['run']
            assert second['status'] == 'waiting_input'
            async with factory.begin() as db:
                run = await db.get(AgentRun, uuid.UUID(second['id']))
                assert not run.execution.get('intentFeedback')
                doc = await db.get(Document, document)
                value = copy.deepcopy(doc.data)
                value['objects']['cat']['assetVersion'] = 2
                doc.data = value
            with pytest.raises(HostError, match='已变化'):
                await service.create(workspace, 'owner', body(document, '改图', first['sessionId'], clarifyRunId=second['id']))
    asyncio.run(scenario())


@pytest.mark.parametrize('fail', [False, True])
def test_s03_s07_dependencies_use_previous_output_and_failure_stops(tmp_path, fail):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, client, redis):
            service.intent = pipeline(multi=True, broken=True)
            service.catalog = service.intent.catalog
            created = await service.create(workspace, 'owner', body(document, '先画一只猫，再生成视频'))
            ident = uuid.UUID(created['run']['id'])
            assert len(created['run']['intentTrace']['steps']) == 2
            await service.tick(ident)
            async with factory.begin() as db:
                jobs = list(await db.scalars(select(Job).where(Job.params['hostRunId'].astext == str(ident))))
                assert len(jobs) == 1 and jobs[0].params['hostStepId'] == 's1_image'
                if fail:
                    jobs[0].status, jobs[0].error_code = 'failed', 'UPSTREAM_ERROR'
            if fail:
                await service.tick(ident)
                current = (await service.get(workspace, 'owner', ident))['run']
                assert current['status'] == 'failed'
                assert current['intentTrace']['step_fail_index'] == 0
                async with factory() as db:
                    jobs = list(await db.scalars(select(Job).where(Job.params['hostRunId'].astext == str(ident))))
                    assert len(jobs) == 1
                    ledgers = list(await db.scalars(select(HostCreditLedger).where(HostCreditLedger.run_id == ident)))
                    assert all(row.state != 'reserved' for row in ledgers)
            else:
                await finish_jobs(factory, client, redis, tmp_path, ident)
                await service.tick(ident)
                async with factory() as db:
                    jobs = list(await db.scalars(select(Job).where(Job.params['hostRunId'].astext == str(ident))))
                    first = next(job for job in jobs if job.params['hostStepId'] == 's1_image')
                    run = await db.get(AgentRun, ident)
                    first_asset = run.execution['intentTrace']['step_results'][0]
                    assert first_asset['status'] == 'completed'
                    output = first.phase_payload['assetId']
                    second = [job for job in jobs if job.params['hostStepId'].startswith('s2_frame')]
                    assert len(second) == 4
                    assert all(job.params['referenceAssetIds'] == [str(output)] for job in second)
                await finish_jobs(factory, client, redis, tmp_path, ident)
                await service.tick(ident)
                async with factory() as db:
                    composition = await db.scalar(select(Job).where(Job.params['hostRunId'].astext == str(ident), Job.kind == 'export'))
                    assert composition is not None
                    composition_id = composition.id
                from backend.services.export_runner import execute_export
                await execute_export(composition_id, factory, tmp_path)
                assert (await service.tick(ident))['status'] == 'awaiting_user_final'
                current = (await service.get(workspace, 'owner', ident))['run']
                assert all(step['status'] == 'completed' for step in current['intentTrace']['step_results'])
                async with factory() as db:
                    saved = await db.get(AgentRun, ident)
                    feedback = saved.execution['intentFeedback'][0]
                    assert feedback['source'] == 'task_success' and 'sequential_candidate' in feedback['tags']
                    composition = await db.get(Job, composition_id)
                    asset = await db.get(Asset, uuid.UUID(composition.phase_payload['assetId']))
                    assert asset.kind == 'video' and (tmp_path / asset.storage_key).read_bytes()[4:8] == b'ftyp'
    asyncio.run(scenario())


def test_e06_unavailable_video_persists_failure_and_clarify_candidate(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, _):
            service.intent = pipeline(video=False, broken=True)
            service.catalog = service.intent.catalog
            await add_image(factory, workspace, document)
            pending = (await service.create(workspace, 'owner', body(document, '动起来', selection=['cat'])))['run']
            failed = (await service.create(workspace, 'owner', body(document, '做成视频', pending['sessionId'],
                clarifyRunId=pending['id'], clarifyOptionId='image_to_multiframe')))['run']
            assert failed['status'] == 'failed' and failed['errorCode'] == 'capability_unavailable'
            assert failed['media'] is None and failed['intentTrace']['decided_l2'] == 'image_to_multiframe'
            async with factory() as db:
                saved = await db.get(AgentRun, uuid.UUID(failed['id']))
                assert saved.execution['intentFeedback'][0]['source'] == 'clarify_click'
                assert not list(await db.scalars(select(HostCreditLedger).where(HostCreditLedger.run_id == saved.id)))
    asyncio.run(scenario())


def test_r03_legacy_path_keeps_sequence_off_and_accepts_first_step_reply(tmp_path):
    from tests.integration.test_host_media import body as legacy_body
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, _):
            service.intent.settings = type(service.intent.settings)()
            pending = (await service.create(workspace, 'owner', legacy_body(document, '先画一只猫再生成视频')))['run']
            assert pending['status'] == 'waiting_input' and pending['media'] is None
            accepted = (await service.create(workspace, 'owner', {
                **legacy_body(document, '先做第一步', key='legacy-reply'), 'clarifyRunId': pending['id']}))['run']
            assert accepted['media']['goalType'] == 'image' and accepted['media']['total'] == 1
    asyncio.run(scenario())


def test_p0_first_only_cutout_uses_confirmed_step_prompt(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, _):
            service.intent = pipeline(broken=True)
            service.catalog = service.intent.catalog
            await add_image(factory, workspace, document)
            pending = (await service.create(workspace, 'owner', body(document,
                '先抠图去背景，再做成视频', selection=['cat'])))['run']
            result = (await service.create(workspace, 'owner', body(document, '先做第一步',
                pending['sessionId'], clarifyRunId=pending['id'], clarifyOptionId='step_first_only')))['run']
            assert result['status'] == 'waiting_approval' and result['canvasAction']['type'] == 'cutout'
            assert result['media'] is None
            async with factory() as db:
                assert (await db.get(AgentRun, uuid.UUID(pending['id']))).status == 'completed'
    asyncio.run(scenario())


@pytest.mark.parametrize('message', ['不要输出视频，换成水彩', '不要生成任何视频，只改成水彩'])
def test_negated_video_admits_only_image_plan(tmp_path, message):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, _):
            service.intent = pipeline()
            service.catalog = service.intent.catalog
            await add_image(factory, workspace, document)
            result = (await service.create(workspace, 'owner', body(document, message, selection=['cat'])))['run']
            assert result['media']['goalType'] == 'image' and len(result['steps']) == 1
            assert result['steps'][0]['tools'] == ['image_edit']
    asyncio.run(scenario())


def test_negated_matting_reply_preserves_clothes_edit(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, _):
            service.intent = pipeline(broken=True)
            service.catalog = service.intent.catalog
            await add_image(factory, workspace, document)
            pending = (await service.create(workspace, 'owner', body(document,
                '不要抠图，只把衣服换成红色', selection=['cat'])))['run']
            result = (await service.create(workspace, 'owner', body(document, '改图', pending['sessionId'],
                clarifyRunId=pending['id'], clarifyOptionId='image_edit')))['run']
            assert not result.get('canvasAction') and result['media']['goalType'] == 'image'
            assert result['steps'][0]['intent'] == '不要抠图，只把衣服换成红色'
    asyncio.run(scenario())


def test_new_request_invalidates_expired_clarification(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, _):
            service.intent = pipeline(l2='text_to_image')
            service.catalog = service.intent.catalog
            pending = (await service.create(workspace, 'owner', body(document, '活一点')))['run']
            async with factory.begin() as db:
                run = await db.get(AgentRun, uuid.UUID(pending['id']))
                execution = copy.deepcopy(run.execution)
                execution['intentDecision']['clarify']['expires_at'] = 0
                run.execution = execution
            result = (await service.create(workspace, 'owner', body(document, '生成一张雪山海报', pending['sessionId'])))['run']
            assert result['media']['goalType'] == 'image'
            assert result['steps'][0]['intent'] == '生成一张雪山海报'
            async with factory() as db:
                assert (await db.get(AgentRun, uuid.UUID(pending['id']))).status == 'cancelled'
    asyncio.run(scenario())
