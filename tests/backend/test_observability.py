"""结构化日志与请求上下文(§九):请求 ID 贯穿响应与日志,并发不串号,密钥脱敏。

请求 ID(HTTP requestId)与业务幂等 ID(clientRef/requestId 参数)相互独立。
"""

import logging

import httpx
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.observability import (
    JsonFormatter,
    get_request_id,
    redact_mapping,
    redact_text,
    set_request_id,
)


def test_error_response_echoes_request_id(root):
    """响应头与错误 payload 均携带请求 ID。"""
    with TestClient(create_app(root)) as client:
        response = client.get("/api/documents/does-not-exist", headers={"X-Request-ID": "req-abc"})
    assert response.headers["X-Request-ID"] == "req-abc"
    assert response.json()["traceId"] == "req-abc"


def test_concurrent_requests_keep_distinct_request_ids(root):
    """并发请求各自携带自己的请求 ID,上下文不串号。"""

    import asyncio

    app = create_app(root)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):

                async def one(i: int):
                    response = await client.get(
                        "/api/documents/none", headers={"X-Request-ID": f"req-{i}"}
                    )
                    return i, response.headers["X-Request-ID"], response.json()["traceId"]

                results = await asyncio.gather(*[one(i) for i in range(20)])
        for i, header_id, trace_id in results:
            assert header_id == f"req-{i}"
            assert trace_id == f"req-{i}"

    asyncio.run(scenario())


def test_access_log_records_context_and_duration(root, caplog):
    """访问日志含 requestId/method/path/status/durationMs 结构化字段。"""
    with TestClient(create_app(root)) as client:
        with caplog.at_level(logging.INFO, logger="frayune.access"):
            client.get("/api/documents/does-not-exist", headers={"X-Request-ID": "req-log"})
    records = [r for r in caplog.records if r.name == "frayune.access"]
    assert records, "缺少访问日志"
    record = records[-1]
    assert record.requestId == "req-log"
    assert record.method == "GET"
    assert record.path == "/api/documents/does-not-exist"
    assert record.status == 404
    assert isinstance(record.durationMs, int)


def test_json_formatter_outputs_structured_line_with_request_id():
    """JSON 格式输出含时间/级别/消息/requestId,不因中文转义。"""
    set_request_id("req-json")
    try:
        record = logging.LogRecord(
            "frayune.access", logging.INFO, __file__, 1, "生成完成 %s", ("任务A",), None
        )
        line = JsonFormatter().format(record)
        import json

        payload = json.loads(line)
        assert payload["message"] == "生成完成 任务A"
        assert payload["requestId"] == "req-json"
        assert payload["level"] == "INFO"
    finally:
        set_request_id("")


def test_redaction_masks_sensitive_keys_and_pairs():
    """日志脱敏:键名命中整体遮蔽,api_key=/Authorization: Bearer 值遮蔽。"""
    assert redact_mapping({"imageApiKey": "sk-1", "prompt": "猫"}) == {
        "imageApiKey": "[REDACTED]",
        "prompt": "猫",
    }
    text = redact_text("请求 imageApiKey=sk-secret 失败;Authorization: Bearer abc; 次数=3")
    assert "sk-secret" not in text and "Bearer abc" not in text
    assert "次数=3" in text
    assert "abc" not in text
    assert "signed-value" not in redact_text("HTTP GET https://cdn.test/file?signature=signed-value")
    assert "db-secret" not in redact_text("postgresql+psycopg://user:db-secret@localhost/db")


def test_request_id_contextvar_is_isolated_across_tasks():
    """ContextVar 在不同 asyncio 任务间互不影响,不会串号。"""

    import asyncio

    async def worker(tag: str):
        set_request_id(tag)
        await asyncio.sleep(0.01)
        return get_request_id()

    async def scenario():
        return await asyncio.gather(*[worker(f"t{i}") for i in range(10)])

    results = asyncio.run(scenario())
    assert results == [f"t{i}" for i in range(10)]
