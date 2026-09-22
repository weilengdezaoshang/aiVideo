"""Real Provider phase runner, invoked by Celery with only a durable job identifier."""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import logging
import time
from pathlib import Path
import uuid

import httpx
from redis.exceptions import RedisError

from backend.config import Config
from backend.errors import ErrorCode
from backend.infrastructure.artifacts import ArtifactSpool
from backend.infrastructure.resilience import CircuitDeferred, RedisResilience, scope_key
from backend.models import GenParams, InitImage, with_reference_weight
from backend.providers.download import download_artifact
from backend.providers.policy import (
    Certainty, Operation, ProviderFailure, ProviderPolicy, bounded_call, classify_failure,
    remaining_seconds,
)
from backend.providers.staged import StagedProvider
from backend.services.execution import ExecutionStore

logger = logging.getLogger(__name__)


async def execute_phase(job_id: uuid.UUID, factory, client: httpx.AsyncClient,
                        redis_client, media_root: Path, credentials: dict,
                        allowed_origins: set[str], policy: ProviderPolicy | None = None,
                        artifact_client: httpx.AsyncClient | None = None) -> dict:
    policy = policy or ProviderPolicy()
    store = ExecutionStore(factory, policy)
    claim = await store.claim(job_id)
    if claim is None:
        return {"skipped": True}
    from backend.observability import set_request_id
    set_request_id(str(claim.params.get("traceId", "")))
    spool = ArtifactSpool(media_root)
    if claim.phase == "persist":
        try:
            await asyncio.to_thread(spool.verify, claim.payload)
        except (OSError, ValueError, KeyError, TypeError):
            await store.defer(claim, 5)
            return {"deferred": "artifact_unavailable"}
        return {"completed": await store.complete(claim, claim.payload)}
    operation = Operation(claim.phase)
    permit = None
    control = RedisResilience(redis_client, policy)
    submit_started = False
    started = time.monotonic()
    try:
        snapshot = dict(claim.snapshot)
        credential_ref = snapshot.pop("credentialRef", "")
        secrets = credentials.get(credential_ref, {})
        # Snapshot owns routing; credential registry may supply only secret values.
        config = Config.model_validate({**snapshot, **{
            key: secrets[key] for key in ("imageApiKey", "videoApiKey") if key in secrets}})
        provider = StagedProvider(config, client)
        params = GenParams.model_validate(claim.params)
        seed = params.seed if params.seed >= 0 else int(hashlib.sha256(str(job_id).encode()).hexdigest()[:8], 16) % (2**31)
        image = _reference(media_root, claim.params.get("referenceStorage"))
        mask = _reference(media_root, claim.params.get("maskStorage"))
        params = with_reference_weight(params, image is not None)
        budget = policy.operation_seconds(operation, video=claim.kind == "video")
        if operation == Operation.SUBMIT and not provider.capabilities(claim.kind).asynchronous:
            budget = remaining_seconds(claim.deadline)
        endpoint = config.comfyUrl if config.provider == "comfyui" else config.cloudBaseUrl
        credential_scope = scope_key(endpoint, credential_ref, "", "credential")
        scope = scope_key(endpoint, credential_ref, params.model, operation.value)
        if config.provider == "cloud" and not (provider.cloud.video_key() if claim.kind == "video" else config.imageApiKey):
            raise ProviderFailure(ErrorCode.UPSTREAM_AUTH, operation=operation, certainty=Certainty.REJECTED)
        try:
            await control.check_credential(credential_scope)
            permit = await control.acquire(scope, budget)
        except RedisError:
            # No new paid submissions when global coordination is unavailable.
            if operation in {Operation.SUBMIT, Operation.PREPARE}:
                await store.defer(claim, 5)
                return {"deferred": "redis"}
            # Existing tasks may reconcile under worker pool and connection pool limits.
        if permit and operation in {Operation.POLL, Operation.RECONCILE, Operation.DOWNLOAD}:
            limit = policy.provider_download_per_minute if operation == Operation.DOWNLOAD else policy.provider_poll_per_minute
            if not await control.allow_rate(scope, limit):
                await store.defer(claim, 5)
                with suppress(RedisError):
                    await control.record(permit, "ignore")
                return {"deferred": "operation_rate"}
        if operation == Operation.SUBMIT:
            model_scope = scope_key(endpoint, credential_ref, params.model, "model")
            try:
                permitted = await control.allow_rate(model_scope, policy.provider_submit_per_minute)
            except RedisError:
                await store.defer(claim, 5)
                return {"deferred": "redis"}
            if not permitted:
                await store.defer(claim, 5)
                if permit:
                    with suppress(RedisError):
                        await control.record(permit, "ignore")
                return {"deferred": "rate"}
            if not await store.begin_submit(claim, model_scope,
                    policy.comfy_capacity if config.provider == "comfyui" else policy.cloud_capacity):
                if permit:
                    await control.record(permit, "ignore")
                return {"deferred": "capacity"}
            submit_started = True

        async def invoke():
            scoped = OperationClient(client, policy.http_timeout(
                budget if operation == Operation.SUBMIT else 30))
            provider = StagedProvider(config, scoped)
            if operation == Operation.PREPARE:
                return await provider.prepare(params, seed, image, mask)
            if operation == Operation.SUBMIT:
                return await provider.submit(params, seed, claim.payload, str(job_id), image, mask)
            if operation in {Operation.POLL, Operation.RECONCILE, Operation.CANCEL}:
                if not claim.external_task_id:
                    raise ValueError("missing upstream task identifier")
                if operation == Operation.CANCEL:
                    return await provider.cancel(claim.external_task_id, claim.kind)
                return await provider.poll(claim.external_task_id, claim.kind)
            if operation == Operation.DOWNLOAD:
                from backend.providers.staged import StageResult
                internal = config.provider == "comfyui"
                origins = set(allowed_origins)
                if internal:
                    trusted = httpx.URL(config.comfyUrl)
                    origins = {str(trusted.copy_with(path="/", query=None, fragment=None)).rstrip("/")}
                downloader = scoped if internal else OperationClient(artifact_client or client, policy.http_timeout(30))
                generated = await download_artifact(downloader, claim.payload, claim.kind, origins, internal=internal)
                return StageResult("persist", {}, generated=generated, finished=True)
            raise ValueError("unsupported generation phase")

        operation_task = asyncio.create_task(bounded_call(operation, claim.deadline, budget, invoke))

        async def keep_alive():
            while True:
                await asyncio.sleep(policy.heartbeat_s)
                try:
                    alive = await store.heartbeat(claim)
                except Exception:
                    alive = False
                if not alive:
                    operation_task.cancel()
                    return

        heartbeat = asyncio.create_task(keep_alive())
        try:
            result = await operation_task
            if result.generated is not None:
                # A successful HTTP response is not proof that its bytes are an image.
                # Validate before publication; malformed completed output never triggers
                # another paid submission, and an explicit terminal result releases slots.
                if claim.kind == "image":
                    from PIL import Image, UnidentifiedImageError
                    from backend.services.media import inspect_media
                    from backend.providers.base import Generated
                    try:
                        meta = await asyncio.to_thread(inspect_media, result.generated.data, "image")
                    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
                        await store.advance(claim, "failed", {"reason": "上游产物不是有效图片"},
                            upstream_finished=result.finished)
                        return {"failed": "INVALID_ARTIFACT"}
                    generated = Generated(result.generated.data, meta["ext"])
                else:
                    generated = result.generated
                artifact = await asyncio.to_thread(spool.save, job_id, claim.epoch, generated)
                completed = await store.complete(claim, artifact)
                outcome = {"completed": completed}
            else:
                advanced = await store.advance(claim, result.phase, result.payload,
                    external_id=result.external_id, delay_s=3 if result.phase == "poll" else 0,
                    upstream_finished=result.finished)
                outcome = {"advanced": advanced, "phase": result.phase}
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        # Business persistence succeeds independently of best-effort success telemetry.
        if permit:
            with suppress(RedisError):
                await control.record(permit, "success")
        return outcome
    except CircuitDeferred as exc:
        await store.defer(claim, exc.seconds)
        return {"deferred": exc.reason}
    except asyncio.CancelledError:
        # Recovery owns uncertain submissions; never turn cancellation into safe retry.
        raise
    except (httpx.HTTPError, TimeoutError, ProviderFailure, ValueError, KeyError) as exc:
        if isinstance(exc, (ValueError, KeyError)):
            failure = ProviderFailure(ErrorCode.INVALID_PARAM, operation=operation,
                certainty=Certainty.UNKNOWN if operation == Operation.SUBMIT and submit_started else Certainty.REJECTED)
        else:
            failure = classify_failure(exc, operation)
        await store.fail(claim, failure)
        if failure.code == ErrorCode.UPSTREAM_AUTH:
            with suppress(RedisError):
                await control.pause_credential(credential_scope)
        if permit:
            outcome = ("auth" if failure.code == ErrorCode.UPSTREAM_AUTH else
                       "rate" if failure.code == ErrorCode.UPSTREAM_RATE_LIMIT else
                       "failure" if failure.availability_failure else "ignore")
            with suppress(RedisError):
                await control.record(permit, outcome,
                                     failure.retry_after if failure.retry_after is not None else 30)
        return {"failed": failure.code.value, "at": datetime.now(timezone.utc).isoformat()}
    finally:
        logger.info("provider_phase_finished", extra={"jobId": str(job_id),
            "executionEpoch": claim.epoch, "phase": claim.phase,
            "elapsedMs": int((time.monotonic() - started) * 1000)})


def _reference(root: Path, value: dict | None) -> InitImage | None:
    if not value:
        return None
    path = (root / value["key"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("invalid reference path")
    if path.stat().st_size > 8 * 1024**2:
        raise ValueError("reference exceeds size limit")
    return InitImage(path.read_bytes(), value["ext"])


class OperationClient:
    """Use a shared connection pool without mutating its timeout across coroutines."""
    def __init__(self, client, timeout):
        self.client, self.timeout = client, timeout

    async def request(self, method, url, **kwargs):
        kwargs["timeout"] = self.timeout
        kwargs["follow_redirects"] = False
        async with self.client.stream(method, url, **kwargs) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 64 * 1024**2:
                    raise ValueError("Provider response exceeds size limit")
            # aiter_bytes already decoded content encoding. Do not decode it twice.
            headers = {key: value for key, value in response.headers.items()
                if key.lower() not in {"content-encoding", "content-length"}}
            return httpx.Response(response.status_code, headers=headers, content=bytes(data), request=response.request)

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)

    def stream(self, method, url, **kwargs):
        kwargs["timeout"] = self.timeout
        return self.client.stream(method, url, **kwargs)
