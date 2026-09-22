import io
from PIL import Image
from backend.storage import Assets


def test_uploaded_names_are_optional_safe_and_persisted(client, root):
    data = io.BytesIO()
    Image.new("RGB", (8, 12)).save(data, "PNG")
    uploaded = client.post(
        "/api/assets", params={"ext": "png", "name": r"C:\fakepath\花.png"}, content=data.getvalue()
    ).json()
    assert uploaded["asset"]["name"] == "花.png"
    ident = uploaded["asset"]["id"]
    assert client.get(f"/api/assets/{ident}").json()["asset"]["name"] == "花.png"
    assert Assets(root / "data" / "assets").get(ident)["name"] == "花.png"
    legacy = client.post("/api/assets?ext=png", content=data.getvalue()).json()
    assert "name" not in legacy["asset"]
    assert legacy["asset"]["width"] == 8
    assert client.get(legacy["urls"]["original"]).status_code == 200
    names = {
        entry["asset"]["id"]: entry["asset"].get("name")
        for entry in client.get("/api/assets").json()["assets"]
    }
    assert names[ident] == "花.png"
