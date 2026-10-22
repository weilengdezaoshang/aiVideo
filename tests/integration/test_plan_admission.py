"""Real PostgreSQL transactions, receipts and CAS; all media uses the staged Mock."""
import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
import uuid

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from backend.agent.admission.policy import IntentContract, Proposal
from backend.agent.admission.service import propose, require_admission
from backend.agent.media.catalog import HostError
from backend.agent.media.planner import plan_intent
from backend.agent.media.service import MediaHost
from backend.agent.orm import (AgentRun, AgentSession, AgentPlanVersion, AgentPlanReview,
    HostCreditAccount, HostCreditLedger)
from backend.infrastructure.orm import Document, Job, Outbox
from backend.services.acceptance import accept_generation
from test_host_media import fixture as media_fixture, body, DB, REDIS, catalog, finish_jobs

pytestmark = pytest.mark.skipif(not DB or not REDIS, reason="explicit isolated migrated PostgreSQL and Redis required")


@asynccontextmanager
async def fixture(tmp_path):
    # Every test owns its binding capacity; intentionally interrupted jobs must not
    # starve later tests that share the same isolated PostgreSQL instance.
    from backend.agent.media.catalog import Catalog
    from backend.agent.media.contracts import CatalogConfig
    async with media_fixture(tmp_path) as values:
        _, workspace, _, host, _, _ = values
        raw = host.catalog.config.model_dump(mode="json")
        for cap in raw["capabilities"]:
            cap["binding"]["version"] = f"admission-test-{workspace}"
        host.catalog = Catalog(CatalogConfig.model_validate(raw))
        yield values


def test_rejected_plan_is_durable_and_has_no_effects(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, host, _, __):
            # Exercise the uniform proposal boundary independently of a planner's early validation.
            async with factory.begin() as db:
                session = AgentSession(workspace_id=workspace, document_id=document,
                    source_document_id=document, principal=str(workspace))
                db.add(session)
                await db.flush()
                plan = plan_intent("生成图片", [], host.catalog).model_dump(mode="json")
                intent = IntentContract(message=plan["goal"], expectedPlan=deepcopy(plan), readableAssets=[],
                    writableNodes=[node for step in plan["steps"] for node in step["targetNodes"]])
                plan["steps"][0]["prompt"] = "篡改后的请求"
                run = AgentRun(session_id=session.id, workspace_id=workspace, client_request_id="rejected",
                    request_hash="a" * 64, principal=str(workspace), message=intent.message,
                    snapshot={"id": str(document), "revision": 1}, route={}, execution={"plan": plan},
                    phase="media", status="failed")
                db.add(run)
                await db.flush()
                result = await propose(db, run, Proposal(writer="user", plan=plan), intent, host.catalog,
                    expected_version=run.state_version)
                ident = run.id
                assert result.verdict == "rejected" and run.execution["admissionId"] is None
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(AgentPlanVersion).where(AgentPlanVersion.run_id == ident)) == 1
                assert await db.scalar(select(func.count()).select_from(AgentPlanReview).join(AgentPlanVersion).where(AgentPlanVersion.run_id == ident)) == 1
                assert await db.scalar(select(func.count()).select_from(HostCreditLedger).where(HostCreditLedger.run_id == ident)) == 0
                assert (await db.get(HostCreditAccount, workspace)).available == 200
                assert (await db.get(Document, document)).revision == 1
                assert await db.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace)) == 0
                outbox = await db.scalar(select(Outbox).where(Outbox.aggregate_id == str(ident)))
                assert outbox.payload == {"runId": str(ident)}
    asyncio.run(scenario())


def test_recovery_reuses_receipt_duplicate_delivery_does_not_charge(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, host, client, redis):
            created = await host.create(workspace, str(workspace), body(document))
            ident = uuid.UUID(created["run"]["id"])
            async with factory() as db:
                admission_id = (await db.get(AgentRun, ident)).execution["admissionId"]
            recovered = MediaHost(factory, catalog(), tmp_path)
            await asyncio.gather(recovered.tick(ident), recovered.tick(ident))
            async with factory() as db:
                assert (await db.get(AgentRun, ident)).execution["admissionId"] == admission_id
                assert await db.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace)) == 4
                assert (await db.get(HostCreditAccount, workspace)).available == 160
            await finish_jobs(factory, client, redis, tmp_path, ident)
            assert (await recovered.tick(ident))["status"] == "awaiting_user_final"
    asyncio.run(scenario())


def test_stale_writer_tampering_and_immutable_database_records(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, host, _, __):
            created = await host.create(workspace, str(workspace), body(document))
            ident = uuid.UUID(created["run"]["id"])
            async with factory.begin() as db:
                _, _, run = await host._locked(db, ident)
                row = await require_admission(db, run)
                with pytest.raises(HostError, match="版本"):
                    await propose(db, run, Proposal.model_validate(row.proposal), IntentContract.model_validate(row.intent),
                        host.catalog, expected_version=run.state_version - 1)
                frozen_id = row.id
                run.execution = {**run.execution, "plan": {**run.execution["plan"], "goal": "篡改"}}
            async with factory.begin() as db:
                with pytest.raises(HostError, match="计划未审核"):
                    await require_admission(db, await db.get(AgentRun, ident))
            assert (await host.tick(ident))["status"] == "needs_attention"
            async with factory.begin() as db:
                with pytest.raises(DBAPIError, match="immutable"):
                    await db.execute(update(AgentPlanVersion).where(AgentPlanVersion.id == frozen_id).values(writer="user"))
    asyncio.run(scenario())


def test_generic_generation_boundary_rejects_forged_host_and_rolls_back(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, _, __, ___):
            with pytest.raises(HostError, match="运行不存在"):
                async with factory.begin() as db:
                    await accept_generation(factory, session=db, workspace_id=workspace, principal=str(workspace),
                        request_id="forged", request_hash="b" * 64, kind="image",
                        params={"prompt": "未审核", "hostRunId": str(uuid.uuid4()), "documentId": str(document)})
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace)) == 0
    asyncio.run(scenario())


def test_crash_before_commit_rolls_back_review_and_reservation(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, host, _, __):
            original = host._wake
            def crash(db, run):
                raise RuntimeError("injected crash before commit")
            host._wake = crash
            with pytest.raises(RuntimeError, match="injected crash"):
                await host.create(workspace, str(workspace), body(document))
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.workspace_id == workspace)) == 0
                assert (await db.get(HostCreditAccount, workspace)).available == 200
                assert (await db.get(Document, document)).revision == 1
            host._wake = original
            assert (await host.create(workspace, str(workspace), body(document)))["run"]["status"] == "queued"
    asyncio.run(scenario())


def test_queued_job_is_checked_again_before_paid_submission(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, host, client, redis):
            from backend.services.generation_runner import execute_phase
            created = await host.create(workspace, str(workspace), body(document, prompt="生成图片"))
            ident = uuid.UUID(created["run"]["id"])
            await host.tick(ident)
            async with factory.begin() as db:
                job = await db.scalar(select(Job).where(Job.workspace_id == workspace))
                job_id = job.id
                job.params = {**job.params, "prompt": "篡改排队任务"}
            # First phase prepares Mock, second attempts submit. Neither phase calls a paid provider.
            for _ in range(3):
                await execute_phase(job_id, factory, client, redis, tmp_path, {}, set())
            async with factory() as db:
                job = await db.get(Job, job_id)
                assert job.status == "failed" and job.error_code == "ADMISSION_REQUIRED"
                assert job.attempt_count == 0 and job.external_task_id is None
    asyncio.run(scenario())


def test_revision_invalidates_old_admission_and_concurrent_writers_use_cas(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (factory, workspace, document, host, _, __):
            from backend.agent.media.catalog import Catalog
            host.catalog = Catalog(host.catalog.config.model_copy(update={"autoStartPreview": False}))
            created = await host.create(workspace, str(workspace), body(document, prompt="生成图片"))
            ident = uuid.UUID(created["run"]["id"])
            async with factory() as db:
                run = await db.get(AgentRun, ident)
                expected_version, original_admission = run.state_version, run.execution["admissionId"]
                row = await db.get(AgentPlanVersion, uuid.UUID(run.execution["planVersionId"]))
                intent = IntentContract.model_validate(row.intent)
                proposed = deepcopy(row.proposal["plan"])
                proposed["steps"][0]["title"] = "用户调整显示标题"
            async def revise():
                try:
                    async with factory.begin() as db:
                        _, _, run = await host._locked(db, ident)
                        return (await propose(db, run, Proposal(writer="user", plan=proposed), intent,
                            host.catalog, expected_version=expected_version)).verdict
                except HostError as exc:
                    return exc.code
            assert sorted(await asyncio.gather(revise(), revise())) == ["STALE_VERSION", "approved"]
            async with factory.begin() as db:
                run = await db.get(AgentRun, ident)
                assert run.execution["admissionId"] != original_admission
                assert (await require_admission(db, run)).version == 2
                saved = run.execution
                run.execution = {**saved, "admissionId": original_admission}
                with pytest.raises(HostError):
                    await require_admission(db, run)
                run.execution = saved
            assert (await host.confirm(workspace, str(workspace), ident, "approve-revised", True))["run"]["status"] == "queued"
            await host.tick(ident)
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace)) == 1
    asyncio.run(scenario())


def test_saved_model_output_is_required_and_is_reused_without_a_model_call(tmp_path):
    async def scenario():
        import json
        from backend.agent.orm import AgentModelCall
        async with fixture(tmp_path) as (factory, workspace, document, host, _, __):
            from backend.agent.media.catalog import Catalog
            host.catalog = Catalog(host.catalog.config.model_copy(update={"autoStartPreview": False}))
            created = await host.create(workspace, str(workspace), body(document, prompt="生成图片"))
            ident = uuid.UUID(created["run"]["id"])
            async with factory.begin() as db:
                _, _, run = await host._locked(db, ident)
                row = await require_admission(db, run)
                plan = deepcopy(row.proposal["plan"])
                plan["steps"][0]["title"] = "模型单子"
                intent = IntentContract.model_validate(row.intent)
                call = AgentModelCall(run_id=ident, ordinal=1, status="completed", execution_epoch=0,
                    messages=[], response={"content": json.dumps({"plan": plan})}, raw_response={"stored": True})
                db.add(call)
                await db.flush()
                call_id = call.id
            # A reconstructed process reads the saved response and issues the proposal once.
            async with factory.begin() as db:
                _, _, run = await MediaHost(factory, host.catalog, tmp_path)._locked(db, ident)
                result = await propose(db, run, Proposal(writer="llm", plan=plan, modelCallId=str(call_id)), intent,
                    host.catalog, expected_version=run.state_version)
                assert result.verdict == "approved"
                await require_admission(db, run)
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(AgentModelCall).where(AgentModelCall.run_id == ident)) == 1
    asyncio.run(scenario())


def test_retry_cannot_exceed_frozen_cumulative_credit_budget(tmp_path):
    async def scenario():
        from backend.agent.media.catalog import Catalog
        from backend.agent.media.contracts import ReviewSettings
        async with fixture(tmp_path) as (factory, workspace, document, host, client, redis):
            host.catalog = Catalog(host.catalog.config.model_copy(update={"admission": ReviewSettings(maxCredits=40)}))
            created = await host.create(workspace, str(workspace), body(document))
            ident = uuid.UUID(created["run"]["id"])
            await host.tick(ident)
            async with factory.begin() as db:
                job = await db.scalar(select(Job).where(Job.workspace_id == workspace,
                    Job.params["hostChildIndex"].as_integer() == 2))
                job.status, job.error_code, job.attempt_count = "failed", "UPSTREAM_TIMEOUT", 1
            await finish_jobs(factory, client, redis, tmp_path, ident)
            assert (await host.tick(ident))["status"] == "failed"
            with pytest.raises(HostError, match="积分预算"):
                await host.retry(workspace, str(workspace), ident,
                    {"clientRequestId": "over-budget", "stepId": "directions", "index": 2})
            async with factory() as db:
                assert (await db.get(HostCreditAccount, workspace)).available == 160
                assert await db.scalar(select(func.count()).select_from(HostCreditLedger).where(HostCreditLedger.run_id == ident)) == 4
    asyncio.run(scenario())


def test_reference_changed_after_review_cannot_be_confirmed(tmp_path):
    async def scenario():
        import io
        from PIL import Image
        from backend.services.media import save_media
        from backend.infrastructure.orm import Asset
        from backend.agent.media.catalog import Catalog
        async with fixture(tmp_path) as (factory, workspace, document, host, _, __):
            stream = io.BytesIO()
            Image.new("RGB", (16, 16), "red").save(stream, format="PNG")
            asset = await save_media(factory, tmp_path, workspace, stream.getvalue(), "image", "png")
            host.catalog = Catalog(host.catalog.config.model_copy(update={"autoStartPreview": False}))
            created = await host.create(workspace, str(workspace), {**body(document, prompt="修改图片"),
                "referenceAssetIds": [str(asset.id)]})
            ident = uuid.UUID(created["run"]["id"])
            async with factory.begin() as db:
                source = await db.get(Asset, asset.id)
                source.sha256 = "0" * 64
            with pytest.raises(HostError, match="参考资产"):
                await host.confirm(workspace, str(workspace), ident, "changed-source", True)
            async with factory() as db:
                assert (await db.get(HostCreditAccount, workspace)).available == 200
                assert (await db.get(Document, document)).revision == 1
    asyncio.run(scenario())


def test_model_proposal_without_saved_output_cannot_create_a_review(tmp_path):
    async def scenario():
        from backend.agent.media.catalog import Catalog
        async with fixture(tmp_path) as (factory, workspace, document, host, _, __):
            host.catalog = Catalog(host.catalog.config.model_copy(update={"autoStartPreview": False}))
            created = await host.create(workspace, str(workspace), body(document, prompt="生成图片"))
            ident = uuid.UUID(created["run"]["id"])
            async with factory.begin() as db:
                _, _, run = await host._locked(db, ident)
                row = await require_admission(db, run)
                plan = deepcopy(row.proposal["plan"])
                plan["steps"][0]["title"] = "未落库的模型计划"
                version = run.state_version
                with pytest.raises(HostError, match="模型输出必须先落库"):
                    await propose(db, run, Proposal(writer="llm", plan=plan),
                        IntentContract.model_validate(row.intent), host.catalog, expected_version=version)
                assert run.state_version == version
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(AgentPlanVersion).where(AgentPlanVersion.run_id == ident)) == 1
    asyncio.run(scenario())
