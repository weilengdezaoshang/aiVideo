from fastapi.testclient import TestClient

from backend.app import create_app


def test_typescript_requires_build_and_uses_original_module_url(tmp_path):
    web = tmp_path / "apps/web"
    (web / "canvas").mkdir(parents=True)
    (web / "canvas/entry.ts").write_text("export const value: number = 1")
    (web / "legacy.js").write_text("export const legacy = true")
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/canvas/entry.js").status_code == 503
        compiled = tmp_path / ".web-build/canvas"
        compiled.mkdir(parents=True)
        (compiled / "entry.js").write_text("export const value = 1")
        response = client.get("/canvas/entry.js")
        assert response.status_code == 200
        assert response.text == "export const value = 1"
        assert "javascript" in response.headers["content-type"]
        assert client.get("/legacy.js").text == "export const legacy = true"


def test_react_bundle_requires_build_and_retains_workspace_url(tmp_path):
    web = tmp_path / "apps/web/workspace"
    web.mkdir(parents=True)
    (web / "app.tsx").write_text("export const App = () => <main />")
    (web / "index.html").write_text('<div id="workspace-root"></div>')
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/workspace/app.js").status_code == 503
        compiled = tmp_path / ".web-build/workspace"
        compiled.mkdir(parents=True)
        (compiled / "app.js").write_text("console.log('bundled React')")
        response = client.get("/workspace/app.js")
        assert response.status_code == 200
        assert "bundled React" in response.text
        assert response.headers["cache-control"] == "no-cache"
        assert client.get("/workspace?tab=canvas").status_code == 200
        assert client.get("/workspace/").status_code == 200
