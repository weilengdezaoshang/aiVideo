import io
import wave


def test_audio_asset_validates_metadata_and_roundtrip(client):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    data = buffer.getvalue()
    response = client.post("/api/assets?kind=audio&ext=wav&name=旁白.wav", content=data)
    assert response.status_code == 201
    result = response.json()
    assert result["asset"]["durationSec"] == 1
    assert result["asset"]["sampleRate"] == 16000
    assert result["asset"]["channels"] == 1
    assert result["asset"]["hasThumbs"] is False
    assert client.get(result["urls"]["original"]).content == data
    assert (
        client.get("/api/assets?kind=audio").json()["assets"][0]["asset"]["id"]
        == result["asset"]["id"]
    )
    for invalid in [b"not wave", data[:-20]]:
        assert client.post("/api/assets?kind=audio&ext=wav", content=invalid).status_code == 400
