"""集成测试环境:真实 PostgreSQL/RabbitMQ 容器;不加载 FastAPI 应用栈。"""

import pytest

from backend.config import ENV


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setenv("SWARMUI_ENV", "development")
    for name in ENV.values():
        monkeypatch.delenv(name, raising=False)
