import io

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from backend.app import create_app, ROOT
from backend.config import ENV


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setenv("SWARMUI_ENV", "development")
    for name in ENV.values():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def root(tmp_path):
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps/web").symlink_to(ROOT / "apps/web", target_is_directory=True)
    # 前端 TS 编译产物(app.js 等模块 URL)与 dev/部署一致,从真实构建目录提供。
    if (ROOT / ".web-build").is_dir():
        (tmp_path / ".web-build").symlink_to(ROOT / ".web-build", target_is_directory=True)
    return tmp_path


@pytest.fixture
def client(root):
    with TestClient(create_app(root)) as client:
        yield client


@pytest.fixture
def png():
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(output, "PNG")
    return output.getvalue()
