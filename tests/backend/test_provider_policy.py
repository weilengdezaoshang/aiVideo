import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.errors import ErrorCode
from backend.infrastructure.artifacts import ArtifactSpool
from backend.providers.base import Generated
from backend.providers.policy import (
    Certainty, Operation, ProviderFailure, ProviderPolicy, bounded_call,
    classify_failure, retry_after_seconds,
)
from backend.providers.staged import StagedProvider
from backend.config import Config
from backend.models import GenParams


def test_pool_timeout_is_not_upstream_failure():
    failure = classify_failure(httpx.PoolTimeout("secret"), Operation.SUBMIT)
    assert failure.certainty == Certainty.NOT_SENT
    assert failure.retryable and not failure.availability_failure
    assert "secret" not in str(failure)


@pytest.mark.parametrize("error", [httpx.ReadTimeout("secret"), httpx.WriteTimeout("secret"), TimeoutError()])
def test_ambiguous_submit_must_not_retry(error):
    failure = classify_failure(error, Operation.SUBMIT)
    assert failure.code == ErrorCode.UPSTREAM_UNKNOWN
    assert not failure.retryable
    assert failure.certainty == Certainty.UNKNOWN


def test_poll_timeout_is_safe_to_retry():
    failure = classify_failure(httpx.ReadTimeout("read"), Operation.POLL)
    assert failure.code == ErrorCode.UPSTREAM_TIMEOUT and failure.retryable


@pytest.mark.parametrize("status,code,counted", [(401, ErrorCode.UPSTREAM_AUTH, False),
    (429, ErrorCode.UPSTREAM_RATE_LIMIT, False), (503, ErrorCode.UPSTREAM_UNAVAILABLE, True)])
def test_status_classification(status, code, counted):
    response = httpx.Response(status, headers={"Retry-After": "42"}, request=httpx.Request("GET", "https://test"))
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        failure = classify_failure(exc, Operation.POLL)
    assert failure.code == code
    assert failure.availability_failure is counted
    if status == 429:
        assert failure.retry_after == 42


def test_deadline_prevents_call_and_does_not_reset():
    calls = []

    async def scenario():
        async def call():
            calls.append(1)
        deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        for _ in range(2):
            with pytest.raises(ProviderFailure) as failure:
                await bounded_call(Operation.SUBMIT, deadline, 30, call)
            assert failure.value.certainty == Certainty.NOT_SENT
        assert calls == []
    asyncio.run(scenario())


def test_wall_clock_timeout_cancels_network_operation():
    cancelled = []

    async def scenario():
        async def call():
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)
        with pytest.raises(ProviderFailure) as failure:
            await bounded_call(Operation.SUBMIT, datetime.now(timezone.utc) + timedelta(seconds=1), .01, call)
        assert failure.value.code == ErrorCode.UPSTREAM_UNKNOWN
        assert cancelled == [True]
    asyncio.run(scenario())


def test_retry_after_and_network_budgets():
    assert retry_after_seconds("NaN") is None
    assert retry_after_seconds("-1") is None
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", datetime(2015, 10, 21, 7, 27, tzinfo=timezone.utc)) == 60
    timeout = ProviderPolicy().http_timeout(1200)
    assert (timeout.pool, timeout.connect, timeout.write, timeout.read) == (2, 5, 30, 1200)


def test_spool_checks_integrity_and_fences_epochs(tmp_path):
    import uuid
    ident = uuid.uuid4()
    spool = ArtifactSpool(tmp_path)
    first = spool.save(ident, 1, Generated(b"first", "png"))
    second = spool.save(ident, 2, Generated(b"second", "png"))
    assert first["storage_key"] != second["storage_key"]
    assert spool.read(ident, 1) == first
    (tmp_path / first["storage_key"]).write_bytes(b"tamper")
    with pytest.raises(ValueError, match="checksum"):
        spool.read(ident, 1)


def test_staged_video_poll_never_submits_again():
    calls = []

    def handle(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"output": {"task_id": "original"}})
        return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED", "video_url": "https://cdn.test/file.mp4"}})

    async def scenario():
        config = Config(provider="cloud", cloudVendor="aliyun", cloudBaseUrl="https://test",
                        videoModel="wan-test", videoApiKey="test-only")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = StagedProvider(config, client)
            params = GenParams(prompt="test", model="wan-test", kind="video", width=1280, height=720, durationSec=5)
            submitted = await provider.submit(params, 1, {}, "request")
            assert submitted.external_id == "original"
            result = await provider.poll(submitted.external_id, "video")
            assert result.phase == "download" and result.finished
        assert calls == ["POST", "GET"]
    asyncio.run(scenario())
