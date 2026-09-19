import pytest
from backend.storyboard import validate_storyboard
from backend.storage import Documents


def board():
    return dict(
        version=1,
        outline="产品广告",
        shots=[
            dict(
                id="shot",
                title="开场",
                visual="产品特写",
                dialogue="",
                camera="推近",
                durationFrames=150,
                nodeId=None,
                locked=False,
            )
        ],
    )


def test_storyboard_roundtrip_and_legacy(tmp_path):
    docs = Documents(tmp_path)
    doc = docs.create("分镜")
    payload = dict(name="分镜", objects={}, order=[], storyboard=board())
    docs.save(doc["id"], payload)
    assert Documents(tmp_path).get(doc["id"])["storyboard"] == board()
    assert (
        docs.save(doc["id"], dict(name="旧客户端", objects={}, order=[]))["storyboard"] == board()
    )
    payload["storyboard"]["shots"][0]["durationFrames"] = -1
    with pytest.raises(ValueError):
        docs.save(doc["id"], payload)
    assert docs.get(doc["id"])["storyboard"] == board()
    payload["storyboard"] = None
    assert "storyboard" not in docs.save(doc["id"], payload)


@pytest.mark.parametrize("frames", [True, 0, -1, 1.5, 108001])
def test_bad_duration(frames):
    raw = board()
    raw["shots"][0]["durationFrames"] = frames
    with pytest.raises(ValueError):
        validate_storyboard(raw)


def test_model_planner_validates_and_assigns_identity(monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from backend.storyboard import plan_storyboard
    from backend.providers import cloud

    async def fake_chat(*args, **kwargs):
        assert kwargs["max_tokens"] == 6000
        return json.dumps(board())

    monkeypatch.setattr(cloud, "chat", fake_chat)
    config = SimpleNamespace(
        provider="cloud", cloudVendor="openai", cloudTextModel="test", imageApiKey="test"
    )
    result = asyncio.run(plan_storyboard("做五秒产品广告", config, None))
    assert result["shots"][0]["id"] != "shot"
    assert result["shots"][0]["nodeId"] is None
    assert result["shots"][0]["locked"] is False
    config.imageApiKey = ""
    with pytest.raises(ValueError, match="配置"):
        asyncio.run(plan_storyboard("广告", config, None))


def test_plan_api_requires_configured_model(client):
    doc = client.post("/api/documents", json={"name": "分镜"}).json()["document"]
    response = client.post(
        "/api/storyboards/plan", json={"documentId": doc["id"], "prompt": "广告"}
    )
    assert response.status_code == 400
    assert "配置" in response.text


def test_version_history_preserved_and_rejects_duplicates():
    raw = board()
    raw["shots"][0]["versions"] = ["first", "second"]
    assert validate_storyboard(raw)["shots"][0]["versions"] == ["first", "second"]
    raw["shots"][0]["versions"].append("first")
    with pytest.raises(ValueError, match="版本"):
        validate_storyboard(raw)


@pytest.mark.parametrize(
    "workflow, expected",
    [("product", "不编造性能"), ("story", "空间连续性"), ("explainer", "待核实")],
)
def test_workflow_reaches_model(monkeypatch, workflow, expected):
    import asyncio
    import json
    from types import SimpleNamespace
    from backend.storyboard import plan_storyboard
    from backend.providers import cloud

    async def fake_chat(client, config, prompt, system, **kwargs):
        assert prompt == "制作十五秒视频"
        assert expected in system
        return json.dumps(board())

    monkeypatch.setattr(cloud, "chat", fake_chat)
    config = SimpleNamespace(
        provider="cloud", cloudVendor="openai", cloudTextModel="test", imageApiKey="test"
    )
    result = asyncio.run(plan_storyboard("制作十五秒视频", config, None, workflow))
    assert result["shots"][0]["visual"] == "产品特写"


@pytest.mark.parametrize("workflow", ["unknown", None, {}, 42])
def test_plan_api_rejects_unknown_workflow(client, workflow):
    doc = client.post("/api/documents", json={"name": "分镜"}).json()["document"]
    response = client.post(
        "/api/storyboards/plan",
        json={"documentId": doc["id"], "prompt": "广告", "workflow": workflow},
    )
    assert response.status_code == 400
    assert "工作流" in response.text
