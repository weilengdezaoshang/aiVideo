"""Analyzer contracts and the vision adapter surface. No database, no network."""

import pytest

from backend.agent.config import AgentRoute
from backend.agent.providers import ModelCallError
from backend.core.recognition.analyzer import AnalysisAnswer, NullAnalyzer, VisionAnalyzer, build_analyzer
from backend.core.recognition.contracts import RecognitionResult, SubjectRecognition, StyleRecognition
from backend.core.execution.vision import VisionAdapter

ROUTE = AgentRoute(id="vision", label="Vision", provider="bailian", model="qwen-vl",
    baseUrl="https://vision.invalid/v1", credentialRef="test-cred", timeoutSeconds=5)


class StubProvider:
    def __init__(self, arguments):
        self.arguments = arguments
        self.calls = []

    async def complete(self, route, messages, tools):
        self.calls.append((route, messages, tools))
        from types import SimpleNamespace
        return SimpleNamespace(tool_calls=[{"name": tools[0]["function"]["name"],
                                           "arguments": self.arguments}])


def _analyzer(arguments):
    return VisionAnalyzer(StubProvider(arguments), ROUTE, timeout=5)


def test_recognition_result_bounds_tags_and_fields():
    with pytest.raises(ValueError):
        StyleRecognition(tags=["ok", " "])
    with pytest.raises(ValueError):
        SubjectRecognition(category="x" * 65)
    result = RecognitionResult(subject=SubjectRecognition(category="猫", description="橘猫"),
        style=StyleRecognition(tags=["水彩"]), overall="一张水彩猫", model="qwen-vl")
    dumped = result.model_dump(mode="json")
    assert dumped["subject"]["category"] == "猫"
    assert dumped["model"] == "qwen-vl"


def test_null_analyzer_is_unavailable_not_failing():
    analyzer = NullAnalyzer()
    assert analyzer.available is False
    with pytest.raises(ModelCallError):
        import asyncio
        asyncio.run(analyzer.recognize(b"png"))


def test_build_analyzer_without_routes_returns_null(monkeypatch):
    monkeypatch.delenv("AIVERO_PRODUCTION_AGENT_ROUTES_FILE", raising=False)
    monkeypatch.setenv("AIVERO_RECOGNITION_ROUTES_FILE", "")
    assert isinstance(build_analyzer(), NullAnalyzer)


def test_vision_analyzer_recognize_parses_structured_call():
    analyzer = _analyzer({"subject": {"category": "猫", "description": "一只橘猫"},
        "style": {"tags": ["水彩"], "description": "淡雅水彩"}, "overall": "水彩橘猫"})
    import asyncio
    result = asyncio.run(analyzer.recognize(b"png-bytes"))
    assert result.subject.category == "猫"
    assert result.style.tags == ["水彩"]
    # Prompt injection hygiene: pixels ride as image_url, question as data.
    route, messages, tools = analyzer.provider.calls[0]
    assert tools[0]["function"]["name"] == "image_recognition"
    assert messages[1]["content"][0]["type"] == "text"
    assert messages[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_analyzer_rejects_extra_tool_calls():
    class TwoCallProvider(StubProvider):
        async def complete(self, route, messages, tools):
            from types import SimpleNamespace
            return SimpleNamespace(tool_calls=[
                {"name": "image_recognition", "arguments": {"subject": {}, "style": {}, "overall": ""}},
                {"name": "other", "arguments": {}}])

    analyzer = VisionAnalyzer(TwoCallProvider(None), ROUTE)
    import asyncio
    with pytest.raises(ModelCallError):
        asyncio.run(analyzer.recognize(b"png"))


def _adapter(tool, analyzer, root):
    return VisionAdapter(tool, analyzer, root)


def _image(tmp_path, size=16):
    path = tmp_path / "media.png"
    path.write_bytes(b"p" * size)
    return path


def _tool(limits=None):
    from backend.core.tools.contracts import Execution, Port, Price, ToolDefinition
    return ToolDefinition(name="image_analyze", version="1", description="d",
        parameters={"type": "object"}, result_schema={"type": "object"}, inputs=[Port(path="image", media_type="image")],
        outputs=[Port(path="analysis", media_type="text")], mode="read_only",
        price=Price(kind="fixed", credits=0),
        execution=Execution(adapter="vision.analyze", result_url_policy="transient", sla_seconds=5),
        failures=[], limits=limits or {"max_image_bytes": 64})


class OkAnalyzer:
    available = True

    def label(self):
        return "stub"

    async def recognize(self, data, *, ext="png"):
        raise AssertionError("recognize is not used by the tool adapter")

    async def answer(self, data, question, *, ext="png", language="zh"):
        assert data
        return AnalysisAnswer(answer="这是水彩风格的猫", prompt_hint="watercolor cat",
            subject={"category": "猫"}, style={"tags": ["水彩"]})


class UnavailableAnalyzer:
    available = False

    def label(self):
        return "null"

    async def recognize(self, data, *, ext="png"):
        raise ModelCallError("ANALYZER_UNAVAILABLE", "x")

    async def answer(self, data, question, *, ext="png", language="zh"):
        raise ModelCallError("ANALYZER_UNAVAILABLE", "x")


def test_vision_adapter_returns_structured_output(tmp_path):
    import asyncio
    path = _image(tmp_path)
    tool = _tool()
    adapter = _adapter(tool, OkAnalyzer(), tmp_path)
    result = asyncio.run(adapter.submit("reg-1", {
        "image": {"asset_id": "a", "storage_key": path.name, "ext": "png", "sha256": "0" * 64},
        "question": "这是什么风格", "_execution_epoch": 1}, None))
    assert result.status == "succeeded"
    assert result.output["answer"] == "这是水彩风格的猫"
    assert result.output["style"]["tags"] == ["水彩"]
    # Crash recovery: the durable marker replays without calling the model again.
    replay = asyncio.run(adapter.query(None, "reg-1"))
    assert replay.output == result.output


def test_vision_adapter_fails_closed(tmp_path):
    import asyncio
    path = _image(tmp_path)
    tool = _tool()
    unavailable = _adapter(tool, UnavailableAnalyzer(), tmp_path)
    params = {"image": {"storage_key": path.name}, "question": "q", "_execution_epoch": 1}
    assert asyncio.run(unavailable.submit("reg-2", params, None)).failure == "provider_down"
    oversize = _adapter(_tool({"max_image_bytes": 4}), OkAnalyzer(), tmp_path)
    assert asyncio.run(oversize.submit("reg-3", params, None)).failure == "param_invalid"
    escape = _adapter(tool, OkAnalyzer(), tmp_path)
    assert asyncio.run(escape.submit("reg-4", {
        "image": {"storage_key": "../media.png"}, "question": "q", "_execution_epoch": 1}, None)
        ).failure == "param_invalid"


def test_request_key_is_content_bound():
    from backend.core.recognition.service import job_request_key
    from backend.infrastructure.orm import Asset

    first = Asset(workspace_id=None, kind="image", ext="png", sha256="a" * 64, storage_key="x")
    same_content = Asset(workspace_id=None, kind="image", ext="png", sha256="a" * 64, storage_key="y")
    same_content.id = first.id
    assert job_request_key(first) == job_request_key(same_content)
    assert job_request_key(first) != job_request_key(
        type(first)(workspace_id=None, kind="image", ext="png", sha256="c" * 64, storage_key="z"))


def test_enqueue_skips_non_images():
    from backend.core.recognition.service import enqueue_recognition
    from backend.infrastructure.orm import Asset
    import asyncio

    class Tx:  # enqueue must not even query for non-image assets
        async def scalar(self, *_a, **_k):
            raise AssertionError("should not query")

    video = Asset(workspace_id=None, kind="video", ext="mp4", sha256="a" * 64, storage_key="v")
    assert asyncio.run(enqueue_recognition(Tx(), workspace_id=None,
        stored_asset=video, requested_by="u", source="upload")) is None
