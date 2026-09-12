"""统一错误契约(§八):稳定错误码、恢复语义、安全响应,HTTP 与 Worker 共用分类。

兼容约束:响应保留既有 `error` 字段(前端文案来源),`code`/`recovery`/`details`
为增量字段;500 响应不携带内部细节,堆栈只进日志。
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.errors import (
    DataCorruptedError,
    ErrorCode,
    Recovery,
    UpstreamAuthError,
    UpstreamUnknownError,
    classify_exception,
    status_contract,
)
from backend.jobs import Jobs
from backend.models import GenParams
from backend.providers.base import Provider
from backend.storage import History
from backend.traces import Traces


def test_client_error_response_keeps_error_field_and_adds_code(root):
    """404 响应保留 error 字段并新增 NOT_FOUND/edit_input 契约字段。"""
    with TestClient(create_app(root)) as client:
        response = client.get("/api/documents/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert body["recovery"] == "edit_input"
    assert isinstance(body.get("error"), str) and body["error"]
    assert body["details"] == {}


def test_param_validation_error_maps_to_invalid_param(root):
    """业务参数校验(ValueError)映射 INVALID_PARAM/edit_input,保留具体文案。"""
    with TestClient(create_app(root)) as client:
        response = client.post("/api/generate", json={"width": 64})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "提示词(prompt)不能为空"
    assert body["code"] == "INVALID_PARAM"
    assert body["recovery"] == "edit_input"


def test_internal_error_response_is_safe_and_logs_stack(root, caplog):
    """未预期异常返回安全兜底;内部细节只进日志堆栈,不进响应。"""
    with TestClient(create_app(root), raise_server_exceptions=False) as client:
        def boom():
            raise RuntimeError("敏感内部细节:数据库密码 x")

        client.app.state.documents.list = boom
        response = client.get("/api/documents")
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "服务器内部错误"
    assert body["code"] == "INTERNAL"
    assert body["recovery"] == "contact"
    assert "数据库密码" not in json.dumps(body)
    assert "数据库密码" in caplog.text  # 堆栈与异常进日志,负责边界记录一次


def test_worker_failure_records_typed_error_code(root):
    """Worker 边界把上游异常分类为稳定 errorCode/recovery,与 HTTP 共用同一分类。"""

    class Upstream401(Provider):
        capacity = 1

        def __init__(self):
            self.name = "fake"

        async def status(self):
            return {"ok": True}

        async def models(self):
            return []

        async def generate(
            self, p, seed, progress, image=None, mask=None, external=None, external_task_id=None
        ):
            request = httpx.Request("POST", "https://upstream/api")
            raise httpx.HTTPStatusError(
                "client error", request=request, response=httpx.Response(401, request=request)
            )

    async def scenario():
        provider = Upstream401()
        jobs = Jobs(provider, History(root), root / "jobs.json", Traces(root / "traces.jsonl"))
        job = jobs.create(GenParams(prompt="x", model="m"))
        while job["status"] in {"queued", "running"}:
            import asyncio

            await asyncio.sleep(0.01)
        assert job["status"] == "failed"
        assert job["errorCode"] == "UPSTREAM_AUTH"
        assert job["recovery"] == "contact"
        await jobs.close()

    import asyncio

    asyncio.run(scenario())


def test_app_error_preserves_reason_chain_and_safe_payload():
    """类型化异常携带稳定 code/recovery 与安全 details,并保留原因链。"""
    cause = ValueError("inner")
    with pytest.raises(UpstreamAuthError) as info:
        try:
            raise cause
        except ValueError as err:
            raise UpstreamAuthError("上游认证失败", details={"jobId": "j1"}) from err
    assert info.value.__cause__ is cause
    assert info.value.to_payload() == {
        "error": "上游认证失败",
        "code": "UPSTREAM_AUTH",
        "recovery": "contact",
        "details": {"jobId": "j1"},
    }


def test_status_contract_maps_legacy_http_status():
    """迁移期:既有 HTTPException 路径按状态码映射 code/recovery。"""
    assert status_contract(404)[0] is ErrorCode.NOT_FOUND
    assert status_contract(429)[0] is ErrorCode.QUEUE_FULL
    assert status_contract(502)[0] is ErrorCode.UPSTREAM_UNAVAILABLE
    assert status_contract(401)[0] is ErrorCode.UNAUTHENTICATED
    assert status_contract(600)[0] is ErrorCode.INTERNAL


def test_classify_exception_uses_types_not_messages():
    """Worker 分类仅依据异常类型与 HTTP 状态码,不解析错误文案。"""
    request = httpx.Request("POST", "https://upstream/api")
    assert classify_exception(ValueError("x")) == (ErrorCode.INVALID_PARAM, Recovery.EDIT_INPUT)
    assert classify_exception(RuntimeError("x")) == (ErrorCode.INTERNAL, Recovery.CONTACT)
    assert classify_exception(httpx.TransportError("x")) == (
        ErrorCode.UPSTREAM_UNAVAILABLE,
        Recovery.RETRY,
    )
    assert classify_exception(
        httpx.HTTPStatusError("x", request=request, response=httpx.Response(429, request=request))
    ) == (ErrorCode.UPSTREAM_RATE_LIMIT, Recovery.RETRY)
    assert classify_exception(UpstreamUnknownError("未知")) == (
        ErrorCode.UPSTREAM_UNKNOWN,
        Recovery.RECONCILE,
    )


def test_corrupted_history_file_fails_fast_instead_of_silent_default(tmp_path):
    """history.json 损坏时快速失败,不静默回空后被下一次保存覆盖原数据。"""
    (tmp_path / "history.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(DataCorruptedError):
        History(tmp_path)
