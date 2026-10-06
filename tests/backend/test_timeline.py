import copy

import pytest

from backend.storage import Documents
from backend.timeline import validate_timeline


def timeline():
    return dict(
        version=1,
        fps=30,
        width=1920,
        height=1080,
        clips=[
            dict(
                id="clip",
                name="镜头",
                source=dict(
                    nodeId="node", kind="video", url="/images/video.mp4", durationFrames=300
                ),
                inFrame=30,
                outFrame=150,
            )
        ],
    )


def test_document_timeline_roundtrip_and_old_client_compatibility(tmp_path):
    docs = Documents(tmp_path)
    doc = docs.create("广告")
    payload = dict(name=doc["name"], objects={}, order=[], timeline=timeline(), baseRevision=1)
    saved = docs.save(doc["id"], payload)
    assert saved["timeline"] == timeline()
    assert Documents(tmp_path).get(doc["id"])["timeline"] == timeline()
    legacy = dict(name="旧客户端改名", objects={}, order=[], baseRevision=2)
    assert docs.save(doc["id"], legacy)["timeline"] == timeline()
    # 显式 null 是撤销初次创建；旧客户端缺字段不能误删。
    removed = docs.save(doc["id"], dict(name="测试", objects={}, order=[], timeline=None))
    assert "timeline" not in removed


def test_document_generation_entities_roundtrip(tmp_path):
    docs = Documents(tmp_path)
    doc = docs.create("生成实体")
    generations = {
        "generation-1": {
            "id": "generation-1",
            "operationId": "operation-1",
            "spec": {"mode": "text-to-image", "params": {"prompt": "猫"}},
            "status": "completed",
            "result": {
                "assetId": "asset-1",
                "src": "/images/cat.png",
                "ext": "png",
                "kind": "image",
            },
            "version": 2,
        }
    }
    payload = {
        "name": doc["name"],
        "objects": {
            "placement-1": {
                "id": "placement-1",
                "kind": "placeholder",
                "generationId": "generation-1",
            }
        },
        "order": ["placement-1"],
        "generations": generations,
        "baseRevision": 1,
    }
    saved = docs.save(doc["id"], payload)
    assert saved["generations"] == generations
    assert Documents(tmp_path).get(doc["id"])["generations"] == generations
    legacy_saved = docs.save(
        doc["id"], {"name": doc["name"], "objects": payload["objects"], "order": payload["order"]}
    )
    assert legacy_saved["generations"] == generations


def test_timeline_canvas_position_roundtrip(tmp_path):
    raw = timeline()
    raw["canvas"] = {"x": -120.5, "y": 800}
    docs = Documents(tmp_path)
    doc = docs.create("时间线")
    docs.save(doc["id"], dict(name=doc["name"], objects={}, order=[], timeline=raw))
    assert Documents(tmp_path).get(doc["id"])["timeline"]["canvas"] == raw["canvas"]
    for position in (None, {}, {"x": True, "y": 0}, {"x": float("nan"), "y": 0}):
        raw["canvas"] = position
        with pytest.raises(ValueError):
            validate_timeline(raw)


@pytest.mark.parametrize("value", [-1, 150, 301, 1.5, True, float("nan")])
def test_invalid_ranges(value):
    raw = timeline()
    raw["clips"][0]["inFrame"] = value
    with pytest.raises(ValueError):
        validate_timeline(raw)


def test_reject_unsupported_and_duplicate_sources():
    raw = timeline()
    raw["clips"].append(copy.deepcopy(raw["clips"][0]))
    with pytest.raises(ValueError):
        validate_timeline(raw)
    raw = timeline()
    raw["clips"][0]["source"]["url"] = "javascript:alert(1)"
    with pytest.raises(ValueError):
        validate_timeline(raw)


def test_api_rejects_invalid_timeline_without_overwriting(client):
    doc = client.post("/api/documents", json={"name": "测试"}).json()["document"]
    payload = dict(name="测试", objects={}, order=[], timeline=timeline(), baseRevision=1)
    assert client.post(f"/api/documents/{doc['id']}", json=payload).status_code == 200
    payload["baseRevision"] = 2
    payload["timeline"]["clips"][0]["outFrame"] = 999
    assert client.post(f"/api/documents/{doc['id']}", json=payload).status_code == 400
    assert client.get(f"/api/documents/{doc['id']}").json()["document"]["timeline"] == timeline()


def test_audio_track_roundtrip_and_invalid_ranges(tmp_path):
    from uuid import uuid4

    ident = str(uuid4())
    raw = timeline()
    raw["audioClips"] = [
        dict(
            id="voice",
            name="旁白",
            source=dict(assetId=ident, url=f"/assets/{ident}/original.wav", durationFrames=300),
            startFrame=30,
            inFrame=60,
            outFrame=180,
            volume=0.5,
            fadeInFrames=15,
            fadeOutFrames=15,
        )
    ]
    docs = Documents(tmp_path)
    doc = docs.create("音频")
    saved = docs.save(doc["id"], dict(name="音频", objects={}, order=[], timeline=raw))
    assert saved["timeline"]["audioClips"] == raw["audioClips"]
    assert Documents(tmp_path).get(doc["id"])["timeline"] == raw
    for bad in [-1, 1.5, True, 108000]:
        invalid = copy.deepcopy(raw)
        invalid["audioClips"][0]["startFrame"] = bad
        with pytest.raises(ValueError):
            validate_timeline(invalid)
