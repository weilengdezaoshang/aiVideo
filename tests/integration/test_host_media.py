"""Opt-in production Host tests: isolated PG/Redis, real staged Mock jobs, no paid credentials."""
import asyncio
from contextlib import asynccontextmanager
import os
import uuid

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select

from backend.agent.media.catalog import Catalog, HostError
from backend.agent.media.contracts import CatalogConfig
from backend.agent.media.credits import grant
from backend.agent.media.service import MediaHost
from backend.agent.orm import AgentRun, HostCreditAccount, HostCreditLedger, HostNodeLease
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Document, Job, Workspace
from backend.services.generation_runner import execute_phase

DB = os.environ.get("AIVERO_DURABLE_TEST_DB")
REDIS = os.environ.get("AIVERO_DURABLE_TEST_REDIS")
pytestmark = pytest.mark.skipif(not DB or not REDIS, reason="explicit isolated migrated PostgreSQL and Redis required")


def catalog():
    return Catalog(CatalogConfig.model_validate({"version": "isolated-v1", "environment": "development",
        "enabled": True, "confirmCreditsAbove": 1000, "capabilities": [
            {"id": name, "enabled": True, "outputType": kind, "maxRefImages": refs,
             "tiers": {"preview": {"credits": 10}, "final": {"credits": 20, "width": 768, "height": 768}},
             "binding": {"version": "mock-v1", "providerSnapshot": {"provider": "mock",
                 "configVersion": "isolated-v1", "cloudModel": "mock-image", "videoModel": "mock-video"}}}
            for name, kind, refs in (("text_to_image", "image", 0), ("image_edit", "image", 1), ("text_to_video", "video", 0))]}))


@asynccontextmanager
async def fixture(tmp_path, balance=200):
    previous_environment = os.environ.get("AIVERO_RUNTIME_ENVIRONMENT")
    os.environ["AIVERO_RUNTIME_ENVIRONMENT"] = "development"
    engine = create_async_database_engine(DatabaseSettings(url=DB))
    factory = create_async_session_factory(engine)
    workspace, document = uuid.uuid4(), uuid.uuid4()
    async with factory.begin() as db:
        db.add(Workspace(id=workspace))
        await db.flush()
        db.add(Document(id=document, workspace_id=workspace, name="isolated", revision=1,
                        data={"objects": {}, "order": []}))
        await grant(db, workspace, "initial-credit", balance, str(workspace))
    service = MediaHost(factory, catalog(), tmp_path)
    client, redis = httpx.AsyncClient(), Redis.from_url(REDIS)
    try:
        yield factory, workspace, document, service, client, redis
    finally:
        await client.aclose()
        await redis.aclose()
        await engine.dispose()
        if previous_environment is None:
            os.environ.pop("AIVERO_RUNTIME_ENVIRONMENT", None)
        else:
            os.environ["AIVERO_RUNTIME_ENVIRONMENT"] = previous_environment


def body(document, prompt="生成四方向图片", revision=1, key="request-1"):
    return {"clientRequestId": key, "documentId": str(document), "message": prompt,
            "baseRevision": revision, "selectedObjectIds": []}


async def finish_jobs(factory, client, redis, root, run_id):
    async with factory() as db:
        jobs = list(await db.scalars(select(Job.id).where(Job.params["hostRunId"].astext == str(run_id))))
    for ident in jobs:
        for _ in range(4):
            await execute_phase(ident, factory, client, redis, root, {}, set())
            async with factory() as db:
                if (await db.get(Job, ident)).status in {"completed", "failed"}:
                    break


def test_four_direction_durable_jobs_credit_reservation_canvas_and_final(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, client, redis):
            created = await service.create(workspace, str(workspace), body(document))
            run_id = uuid.UUID(created["run"]["id"])
            replay = await service.create(workspace, str(workspace), body(document))
            assert replay["replayed"] and replay["run"]["id"] == str(run_id)
            async with factory() as db:
                assert (await db.get(HostCreditAccount, workspace)).available == 160
                assert await db.scalar(select(func.count()).select_from(HostNodeLease).where(HostNodeLease.run_id == run_id)) == 4
            await service.tick(run_id)
            await service.tick(run_id)  # duplicate delivery does not enqueue/charge again
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(Job).where(Job.params["hostRunId"].astext == str(run_id))) == 4
            await finish_jobs(factory, client, redis, tmp_path, run_id)
            assert (await service.tick(run_id))["status"] == "awaiting_user_final"
            async with factory() as db:
                doc = await db.get(Document, document)
                assert len(doc.data["objects"]) == 4
                assert all(node["kind"] == "image" and node["assetVersion"] == 1 and not node.get("busyRunId")
                           for node in doc.data["objects"].values())
                assert sum(await db.scalars(select(HostCreditLedger.charged).where(HostCreditLedger.run_id == run_id))) == 40
                revision = doc.revision
            final = await service.final(workspace, str(workspace), run_id,
                {"clientRequestId": "final-1", "baseRevision": revision})
            final_id = uuid.UUID(final["run"]["id"])
            assert final_id != run_id
            assert final["run"]["media"]["tier"] == "final"
            assert final["run"]["media"]["previewRunId"] == str(run_id)
            await service.tick(final_id)
            await finish_jobs(factory, client, redis, tmp_path, final_id)
            assert (await service.tick(final_id))["status"] == "completed"
            async with factory() as db:
                assert (await db.get(HostCreditAccount, workspace)).available == 80
                assert not list(await db.scalars(select(HostNodeLease).where(HostNodeLease.run_id.in_([run_id, final_id]))))
    asyncio.run(scenario())


def test_insufficient_reservation_starts_no_jobs_and_duplicate_grant_is_idempotent(tmp_path):
    async def scenario():
        async with fixture(tmp_path, balance=10) as (factory, workspace, document, service, _, __):
            with pytest.raises(HostError, match="额度不足"):
                await service.create(workspace, str(workspace), body(document))
            async with factory.begin() as db:
                assert await db.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.workspace_id == workspace)) == 0
                assert await db.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace)) == 0
                assert (await grant(db, workspace, "initial-credit", 10, str(workspace)))["replayed"]
                assert (await db.get(HostCreditAccount, workspace)).available == 10
    asyncio.run(scenario())


def test_cancel_before_submit_refunds_and_late_completed_receipt_charges_without_canvas_write(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, client, redis):
            created = await service.create(workspace, str(workspace), body(document, "生成一张图片"))
            run_id = uuid.UUID(created["run"]["id"])
            await service.tick(run_id)
            await service.cancel(workspace, str(workspace), run_id)
            await service.tick(run_id)  # request provider cancellation
            assert (await service.tick(run_id))["status"] == "cancelled"
            async with factory() as db:
                assert (await db.get(HostCreditAccount, workspace)).available == 200
                revision = (await db.get(Document, document)).revision
            late = await service.create(workspace, str(workspace), body(document, "生成一张图片", revision, "late"))
            late_id = uuid.UUID(late["run"]["id"])
            await service.tick(late_id)
            await finish_jobs(factory, client, redis, tmp_path, late_id)
            await service.cancel(workspace, str(workspace), late_id)
            assert (await service.tick(late_id))["status"] == "cancelled"
            async with factory() as db:
                assert (await db.get(HostCreditAccount, workspace)).available == 190
                ledger = await db.scalar(select(HostCreditLedger).where(HostCreditLedger.run_id == late_id))
                assert ledger.state == "cancelled_with_charge" and ledger.charged == 10
                doc = await db.get(Document, document)
                assert not any(node.get("assetId") for node in doc.data["objects"].values())
    asyncio.run(scenario())


def test_authenticated_host_api_csrf_creator_isolation_and_busy_node_guard(tmp_path):
    from test_production_api import api
    from backend.services.identity import CSRF_COOKIE, SESSION_COOKIE, issue_session
    from backend.infrastructure.orm import WorkspaceMember
    from backend.services.documents import DatabaseDocuments
    async def scenario():
        async with api(tmp_path) as (app, client, workspace):
            app.state.host.catalog = catalog()
            from pydantic import SecretStr
            token = 'isolated-billing-grant-token-32-characters'
            app.state.settings = app.state.settings.model_copy(update={'host_credit_grant_token': SecretStr(token)})
            assert (await client.post('/api/host/credits/grant', json={"clientRequestId": "gift", "amount": 100})).status_code == 403
            assert (await client.post('/api/host/credits/grant', json={"clientRequestId": "gift", "amount": 100}, headers={'authorization': 'Bearer ' + token})).status_code == 200
            created = await client.post('/api/documents', json={"name": "host-test"})
            document = created.json()['document']
            denied = await client.post('/api/host/turn', json=body(document['id']), headers={"x-csrf-token": "wrong"})
            assert denied.status_code == 403
            accepted = await client.post('/api/host/turn', json=body(document['id']))
            assert accepted.status_code == 202
            run_id = accepted.json()['run']['id']
            assert (await client.post('/api/host/turn', json=body(document['id']))).json()['replayed']
            doc = await DatabaseDocuments(app.state.factory).get(workspace, uuid.UUID(document['id']))
            deleted = await client.post(f"/api/documents/{document['id']}", json={"objects": {}, "order": [], "baseRevision": doc['revision']})
            assert deleted.status_code == 409 and 'node_busy' in deleted.json()['error']
            other = 'other-editor-' + uuid.uuid4().hex
            async with app.state.factory.begin() as db:
                db.add(WorkspaceMember(workspace_id=workspace, principal_id=other, role='editor'))
            token, csrf = await issue_session(app.state.factory, other)
            headers = {"cookie": f"{SESSION_COOKIE}={token}; {CSRF_COOKIE}={csrf}", "x-csrf-token": csrf}
            assert (await client.get(f'/api/host/runs/{run_id}', headers=headers)).status_code == 404
            assert (await client.get(f'/api/agent/runs/{run_id}', headers=headers)).status_code == 404
            snapshot = await client.get(f"/api/documents/{document['id']}/host", headers=headers)
            assert snapshot.json()['runs'] == []
            assert (await client.post('/api/host/credits/grant', json={"clientRequestId": "bad", "amount": 100}, headers=headers)).status_code == 403
            assert (await client.post(f'/api/host/runs/{run_id}/cancel', headers=headers)).status_code == 403
            public = (await client.get('/api/host/capabilities')).text
            assert 'credentialRef' not in public and 'providerSnapshot' not in public and 'mock-image' not in public
            # Distinct principals receive independent Host sessions for the shared canvas.
            second = await client.post('/api/host/turn', json=body(document['id'], '生成一张图片', doc['revision'], 'other-run'), headers=headers)
            assert second.status_code == 202
            assert second.json()['run']['sessionId'] != accepted.json()['run']['sessionId']
    asyncio.run(scenario())


def test_multiframe_persisted_dependencies_produce_playable_interpolated_video(tmp_path):
    import io
    from PIL import Image
    from backend.agent.media.contracts import Capability
    from backend.services.media import save_media
    from backend.services.export_runner import execute_export
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, client, redis):
            cap = Capability.model_validate({"id": "frames_to_video", "enabled": True, "outputType": "video",
                "maxRefImages": 16, "tiers": {"preview": {"credits": 0, "width": 128, "height": 128, "durationSec": 2, "fps": 8},
                    "final": {"credits": 0, "width": 256, "height": 256}},
                "binding": {"version": "optical-flow-v1", "engine": "frames"}})
            service.catalog = Catalog(service.catalog.config.model_copy(update={"capabilities": [*service.catalog.config.capabilities, cap]}))
            buffer = io.BytesIO()
            Image.new('RGB', (512, 512), 'blue').save(buffer, 'PNG')
            source = await save_media(factory, tmp_path, workspace, buffer.getvalue())
            request = {**body(document, '根据参考图生成视频'), "referenceAssetIds": [str(source.id)]}
            created = await service.create(workspace, str(workspace), request)
            run_id = uuid.UUID(created['run']['id'])
            await service.tick(run_id)
            async with factory() as db:
                assert len(list(await db.scalars(select(Job).where(Job.params['hostRunId'].astext == str(run_id))))) == 4
            await finish_jobs(factory, client, redis, tmp_path, run_id)
            await service.tick(run_id)
            async with factory() as db:
                composition = await db.scalar(select(Job).where(Job.params['hostRunId'].astext == str(run_id), Job.kind == 'export'))
                assert composition and len(composition.params['frameSources']) == 4
                ident = composition.id
            await execute_export(ident, factory, tmp_path)
            assert (await service.tick(run_id))['status'] == 'awaiting_user_final'
            async with factory() as db:
                composition = await db.get(Job, ident)
                assert composition.status == 'completed'
                from backend.infrastructure.orm import Asset
                artifact = await db.get(Asset, uuid.UUID(composition.phase_payload['assetId']))
                assert artifact.kind == 'video' and artifact.ext == 'mp4'
                assert (tmp_path / artifact.storage_key).read_bytes()[4:8] == b'ftyp'
    asyncio.run(scenario())


@pytest.mark.parametrize('mode,expected,badge', [('soft', 'awaiting_user_final', 'review_soft_fail'), ('hard', 'failed', None), ('blocked', 'blocked', None)])
def test_real_reviewer_contract_soft_hard_and_no_false_stub_review(tmp_path, mode, expected, badge):
    import json
    from backend.agent.config import AgentRoute
    from backend.agent.media.reviewer import review_one
    from backend.agent.providers import ModelResponse
    from backend.agent.orm import AgentModelCall
    class VisionFixture:
        def __init__(self):
            self.calls = 0
        async def complete(self, route, messages, schemas):
            self.calls += 1
            assert route.model == 'isolated-vision'
            assert schemas[0]['function']['name'] == 'review_result'
            assert any(item.get('type') == 'image_url' and item['image_url']['url'].startswith('data:image/jpeg;base64,')
                       for item in messages[1]['content'])
            args = {'passed': False, 'score': 0.2, 'issues': ['构图不符合需求'], 'contentBlocked': mode == 'blocked'}
            return ModelResponse(None, [{'id': 'verdict', 'name': 'review_result', 'arguments': args,
                'raw_arguments': json.dumps(args)}], {'total_tokens': 7}, 'tool_calls', {}, 'isolated-review')
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, client, redis):
            route = AgentRoute(id='vision', label='Vision', provider='bailian', model='isolated-vision',
                baseUrl='https://isolated.invalid/v1', credentialRef='fixture-only')
            service.catalog = Catalog(service.catalog.config.model_copy(update={'reviewMode': 'hard' if mode == 'blocked' else mode, 'reviewer': route}))
            created = await service.create(workspace, str(workspace), body(document, '生成一张图片'))
            ident = uuid.UUID(created['run']['id'])
            await service.tick(ident)
            await finish_jobs(factory, client, redis, tmp_path, ident)
            assert (await service.tick(ident))['status'] == 'running'
            model = VisionFixture()
            assert await review_one(service, ident, model)
            assert not await review_one(service, ident, model)
            assert (await service.tick(ident))['status'] == expected
            assert model.calls == 1
            async with factory() as db:
                calls = list(await db.scalars(select(AgentModelCall).where(AgentModelCall.run_id == ident)))
                assert len(calls) == 1 and calls[0].status == 'completed'
                assert calls[0].usage['total_tokens'] == 7
                if mode == 'blocked':
                    from backend.infrastructure.orm import Asset
                    job = await db.scalar(select(Job).where(Job.params['hostRunId'].astext == str(ident)))
                    asset = await db.get(Asset, uuid.UUID(job.phase_payload['assetId']))
                    assert asset.details['hostBlocked'] and job.error_code == 'CONTENT_BLOCKED'
                nodes = (await db.get(Document, document)).data['objects']
                node = next(iter(nodes.values()))
                if badge:
                    assert node['badge'] == badge and node['qualityIssue']
                else:
                    assert node['kind'] == 'error' and not node.get('assetId')
    asyncio.run(scenario())


def test_required_input_moderation_blocks_before_media_jobs_and_refunds_reservation(tmp_path):
    import json
    from backend.agent.config import AgentRoute
    from backend.agent.media.reviewer import review_one
    from backend.agent.providers import ModelResponse
    class BlockFixture:
        async def complete(self, route, messages, schemas):
            args = {'passed': False, 'score': 0, 'issues': ['内容审核未通过'], 'contentBlocked': True}
            return ModelResponse(None, [{'id': 'verdict', 'name': 'review_result', 'arguments': args,
                'raw_arguments': json.dumps(args)}], {'total_tokens': 7}, 'tool_calls', {}, None)
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, __):
            route = AgentRoute(id='moderate', label='Moderation', provider='bailian', model='isolated-vision',
                baseUrl='https://isolated.invalid/v1', credentialRef='fixture-only')
            caps = [cap.model_copy(update={'moderation': 'required'}) for cap in service.catalog.config.capabilities]
            service.catalog = Catalog(service.catalog.config.model_copy(update={'reviewer': route, 'capabilities': caps}))
            created = await service.create(workspace, str(workspace), body(document))
            ident = uuid.UUID(created['run']['id'])
            await service.tick(ident)
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace)) == 0
            assert await review_one(service, ident, BlockFixture())
            assert (await service.tick(ident))['status'] == 'blocked'
            async with factory() as db:
                assert (await db.get(HostCreditAccount, workspace)).available == 200
                assert await db.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace)) == 0
                assert not any(node.get('assetId') for node in (await db.get(Document, document)).data['objects'].values())
    asyncio.run(scenario())


def test_partial_failure_retry_is_per_index_idempotent_and_does_not_repeat_successful_jobs(tmp_path):
    from backend.agent.orm import AgentStep
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, client, redis):
            created = await service.create(workspace, str(workspace), body(document))
            ident = uuid.UUID(created['run']['id'])
            await service.tick(ident)
            async with factory.begin() as db:
                jobs = list(await db.scalars(select(Job).where(Job.params['hostRunId'].astext == str(ident)).order_by(Job.params['hostChildIndex'].as_integer())))
                failed_id = jobs[2].id
                jobs[2].status, jobs[2].error_code = 'failed', 'UPSTREAM_TIMEOUT'
            await finish_jobs(factory, client, redis, tmp_path, ident)
            assert (await service.tick(ident))['status'] == 'failed'
            request = {'clientRequestId': 'retry-one', 'stepId': 'directions', 'index': 2}
            retried = await service.retry(workspace, str(workspace), ident, request)
            assert retried['run']['status'] == 'queued'
            assert (await service.retry(workspace, str(workspace), ident, request))['replayed']
            await service.tick(ident)
            async with factory() as db:
                jobs = list(await db.scalars(select(Job).where(Job.params['hostRunId'].astext == str(ident))))
                assert len(jobs) == 5
                assert sum(job.params['hostChildIndex'] == 2 for job in jobs) == 2
                assert (await db.get(Job, failed_id)).status == 'failed'
            await finish_jobs(factory, client, redis, tmp_path, ident)
            assert (await service.tick(ident))['status'] == 'awaiting_user_final'
            async with factory() as db:
                step = await db.scalar(select(AgentStep).where(AgentStep.run_id == ident))
                assert [child['attempt'] for child in step.execution['children']] == [1, 1, 2, 1]
                assert (await db.get(HostCreditAccount, workspace)).available == 160
    asyncio.run(scenario())


def test_clarification_is_persisted_status_questions_do_not_cancel_and_revisions_cancel_before_failed_new_plan(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, __):
            clarification = await service.create(workspace, str(workspace), body(document, '让它动起来'))
            assert clarification['run']['status'] == 'waiting_input'
            assert (await service.snapshot(workspace, str(workspace), document))['runs'][0]['waiting']['question']
            resolved = await service.create(workspace, str(workspace), {**body(document, '视频', key='answer'),
                'clarifyRunId': clarification['run']['id']})
            assert resolved['run']['media']['goalType'] == 'video'
            async with factory() as db:
                doc = await db.get(Document, document)
                revision = doc.revision
            status = await service.create(workspace, str(workspace), {**body(document, '进度？', revision, 'status'),
                'supersedeRunId': resolved['run']['id']})
            assert status['run']['status'] == 'completed' and '0/1' in status['run']['reply']
            assert (await service.get(workspace, str(workspace), uuid.UUID(resolved['run']['id'])))['run']['status'] == 'queued'
            with pytest.raises(HostError):
                await service.create(workspace, str(workspace), {**body(document, '生成配音视频', revision, 'revise'),
                    'supersedeRunId': resolved['run']['id']})
            assert (await service.get(workspace, str(workspace), uuid.UUID(resolved['run']['id'])))['run']['status'] == 'canceling'
    asyncio.run(scenario())


def test_unknown_receipt_not_resubmitted_and_cancel_timeout_keeps_reservation(tmp_path):
    from datetime import timedelta
    from backend.agent.util import now
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, __):
            created = await service.create(workspace, str(workspace), body(document, "生成一张图片"))
            ident = uuid.UUID(created["run"]["id"])
            await service.tick(ident)
            async with factory.begin() as db:
                job = await db.scalar(select(Job).where(Job.params["hostRunId"].astext == str(ident)))
                job.status, job.external_task_id, job.attempt_count = "unknown", "isolated-provider-task", 1
                job_id = job.id
            assert (await service.tick(ident))["status"] == "needs_attention"
            await service.tick(ident)
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(Job).where(Job.params["hostRunId"].astext == str(ident))) == 1
            await service.cancel(workspace, str(workspace), ident)
            async with factory.begin() as db:
                run = await db.get(AgentRun, ident, with_for_update=True)
                run.execution = {**run.execution, "cancelDeadline": (now() - timedelta(seconds=1)).isoformat()}
            assert (await service.tick(ident))["status"] == "cancelled"
            async with factory.begin() as db:
                ledger = await db.scalar(select(HostCreditLedger).where(HostCreditLedger.run_id == ident))
                assert ledger.state == "reserved"
                assert (await db.get(HostCreditAccount, workspace)).available == 190
                assert not list(await db.scalars(select(HostNodeLease).where(HostNodeLease.run_id == ident)))
                job = await db.get(Job, job_id)
                job.status, job.error_code, job.finished_at = "failed", "CANCELLED", now()
            await service.reconcile_terminal_credits()
            await service.reconcile_terminal_credits()
            async with factory() as db:
                ledger = await db.scalar(select(HostCreditLedger).where(HostCreditLedger.run_id == ident))
                assert ledger.state == "cancelled_with_charge" and ledger.charged == 10
                assert (await db.get(HostCreditAccount, workspace)).available == 190
                assert all(not node.get("assetId") for node in (await db.get(Document, document)).data["objects"].values())
    asyncio.run(scenario())


def test_high_cost_confirmation_rejection_never_submits(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, __):
            service.catalog = Catalog(service.catalog.config.model_copy(update={"confirmCreditsAbove": 5}))
            created = await service.create(workspace, str(workspace), body(document, "生成一张图片"))
            ident = uuid.UUID(created["run"]["id"])
            assert created["run"]["status"] == "awaiting_confirm"
            rejected = await service.confirm(workspace, str(workspace), ident, "reject", False)
            assert rejected["run"]["status"] == "cancelled"
            assert (await service.confirm(workspace, str(workspace), ident, "reject", False))["replayed"]
            async with factory() as db:
                assert not list(await db.scalars(select(Job).where(Job.workspace_id == workspace)))
                assert (await db.get(HostCreditAccount, workspace)).available == 200
    asyncio.run(scenario())


def test_recreated_host_recovers_and_node_lease_prevents_competing_writer(tmp_path):
    from backend.agent.media import canvas
    from backend.agent.media.contracts import MediaPlan
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, __):
            created = await service.create(workspace, str(workspace), body(document, '生成一张图片'))
            first = uuid.UUID(created['run']['id'])
            # Simulate a process restart before any job is submitted.
            recovered = MediaHost(factory, catalog(), tmp_path)
            assert (await recovered.get(workspace, str(workspace), first))['run']['status'] == 'queued'
            async with factory() as db:
                revision = (await db.get(Document, document)).revision
            second = uuid.UUID((await recovered.create(workspace, str(workspace), body(document, '生成一张图片', revision, 'second')))['run']['id'])
            with pytest.raises(HostError) as busy:
                async with factory.begin() as db:
                    doc, _, run = await recovered._locked(db, second)
                    source = await db.get(AgentRun, first)
                    source_plan = MediaPlan.model_validate(source.execution['plan'])
                    steps = await recovered._steps(db, run)
                    plan = MediaPlan.model_validate(run.execution['plan'])
                    conflict = plan.model_copy(update={'steps': [plan.steps[0].model_copy(update={'targetNodes': source_plan.steps[0].targetNodes})]})
                    await canvas.allocate(db, doc, run, steps, conflict, catalog())
            assert busy.value.code == 'node_busy'
            async with factory.begin() as db:
                doc = await db.get(Document, document, with_for_update=True)
                run = await db.get(AgentRun, first)
                target = run.execution['plan']['steps'][0]['targetNodes'][0]
                doc.data = {**doc.data, 'objects': {key: val for key, val in doc.data['objects'].items() if key != target},
                            'order': [key for key in doc.data['order'] if key != target]}
            assert (await recovered.tick(first))['status'] == 'failed'
            async with factory() as db:
                assert not list(await db.scalars(select(Job).where(Job.params['hostRunId'].astext == str(first))))
                assert not list(await db.scalars(select(HostNodeLease).where(HostNodeLease.run_id == first)))
    asyncio.run(scenario())


def test_agent_request_context_uses_canonical_run_and_replay(tmp_path):
    """The legacy request snapshot reuses AgentRun, ledger and the existing Worker."""
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, client, redis):
            from backend.services.media import save_media
            from backend.infrastructure.orm import AssetReference
            import io
            from PIL import Image
            buffer = io.BytesIO()
            Image.new('RGB', (512, 512), 'blue').save(buffer, 'PNG')
            source = await save_media(factory, tmp_path, workspace, buffer.getvalue())
            async with factory.begin() as db:
                doc = await db.get(Document, document, with_for_update=True)
                doc.data = {"objects": {"cat": {"id": "cat", "kind": "image", "assetId": str(source.id),
                    "x": 11, "y": 22, "width": 512, "height": 512, "rotation": 13, "name": "private label"}}, "order": ["cat"]}
                db.add(AssetReference(asset_id=source.id, owner_type="document", owner_id=str(document)))
            request_id = str(uuid.uuid4())
            request = {**body(document, "画一只狗", key=request_id), "selectedObjectIds": ["cat"]}
            request["context"] = {"schema_version": 1, "request_id": request_id,
                "conversation_id": str(uuid.uuid4()), "doc_id": str(document), "doc_version": 1,
                "client_id": "test-client", "tab_id": "test-tab", "client_ts": 1,
                "message_text": request["message"], "selection": {"ids": ["cat"], "count": 1, "truncated": False},
                "viewport": {"bbox": [0, 0, 1024, 720], "zoom": 1}}
            created = await service.create(workspace, str(workspace), request)
            run_id = uuid.UUID(created["run"]["id"])
            replay = await service.create(workspace, str(workspace), request)
            assert replay["replayed"] and replay["run"]["id"] == str(run_id)
            async with factory() as db:
                run = await db.get(AgentRun, run_id)
                assert run.execution["inputAssetIds"] == []  # C04: selected cat is not a reference
                assert run.snapshot["context"]["targets_source"] == "no-ref"
                assert "private label" not in str(run.snapshot["context"])
                assert (await db.get(HostCreditAccount, workspace)).available == 190
                doc = await db.get(Document, document)
                assert doc.data["objects"]["cat"]["rotation"] == 13
            await service.tick(run_id)
            await finish_jobs(factory, client, redis, tmp_path, run_id)
            assert (await service.tick(run_id))["status"] == "awaiting_user_final"
            async with factory() as db:
                doc = await db.get(Document, document)
                assert doc.data["objects"]["cat"]["assetId"] == str(source.id)
                output = [node for key, node in doc.data["objects"].items() if key != "cat"]
                assert len(output) == 1 and output[0]["kind"] == "image" and output[0].get("assetId")
            # Changing the frozen context on retry cannot submit another paid job.
            changed = {**request, "context": {**request["context"], "tab_id": "other-tab"}}
            with pytest.raises(HostError) as error:
                await service.create(workspace, str(workspace), changed)
            assert error.value.code == "idempotency_conflict"
    asyncio.run(scenario())


def test_canvas_cutout_handoff_is_durable_free_and_only_completed_after_saved_result(tmp_path):
    from backend.infrastructure.orm import Asset

    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, service, _, _):
            source, output = uuid.uuid4(), uuid.uuid4()
            async with factory.begin() as db:
                for ident in (source, output):
                    db.add(Asset(id=ident, workspace_id=workspace, kind="image", ext="png",
                                 storage_key=f"{ident}.png", sha256=ident.hex * 2, bytes=10, details={}))
                doc = await db.get(Document, document)
                doc.data = {"objects": {"image": {"id": "image", "kind": "image", "assetId": str(source)}}, "order": ["image"]}
            raw = {**body(document, "帮这张图抠图"), "selectedObjectIds": ["image"]}
            result = await service.create(workspace, "owner", raw)
            run_id = uuid.UUID(result["run"]["id"])
            assert result["run"]["status"] == "waiting_approval"
            assert result["run"]["canvasAction"]["sourceObjectId"] == "image"
            assert result["run"]["media"] is None
            assert (await service.create(workspace, "owner", raw))["replayed"]
            async with factory() as db:
                assert not list(await db.scalars(select(Job.id).where(Job.workspace_id == workspace)))
                assert not list(await db.scalars(select(HostCreditLedger.id).where(HostCreditLedger.run_id == run_id)))
            with pytest.raises(HostError):
                await service.complete_canvas_action(workspace, "owner", run_id,
                    {"resultObjectId": "missing", "baseRevision": 1})
            with pytest.raises(HostError):
                await service.complete_canvas_action(workspace, "other", run_id,
                    {"resultObjectId": "result", "baseRevision": 1})
            async with factory.begin() as db:
                doc = await db.get(Document, document)
                doc.revision = 2
                doc.data = {**doc.data, "objects": {**doc.data["objects"], "result": {
                    "id": "result", "kind": "image", "assetId": str(output),
                    "lineage": {"fromId": "image", "params": {"operation": "cutout"}},
                    "cutout": {"originalAssetId": str(source)}}}, "order": ["image", "result"]}
            receipt = {"resultObjectId": "result", "baseRevision": 2}
            complete = await service.complete_canvas_action(workspace, "owner", run_id, receipt)
            assert complete["run"]["status"] == "completed"
            assert complete["run"]["canvasAction"]["resultObjectId"] == "result"
            assert (await service.complete_canvas_action(workspace, "owner", run_id, receipt))["replayed"]
            second = await service.create(workspace, "owner", {**raw, "clientRequestId": "cancel-cutout", "baseRevision": 2})
            cancelled = await service.cancel(workspace, "owner", uuid.UUID(second["run"]["id"]))
            assert cancelled["run"]["status"] == "cancelled"
            with pytest.raises(HostError):
                await service.complete_canvas_action(workspace, "owner", uuid.UUID(second["run"]["id"]), receipt)
    asyncio.run(scenario())
