"""开发 Compose 编排(补充要求 §十三):服务齐备、镜像版本固定、健康检查显式。

本地以文本断言把关;完整栈启动验证为 `docker compose -f compose.dev.yaml up`
(依赖 Docker 环境,CI 中执行,不在单测内起真实容器)。
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
COMPOSE = ROOT / "compose.dev.yaml"


def test_compose_file_exists_with_required_services():
    text = COMPOSE.read_text(encoding="utf-8")
    for service in ("api", "worker", "beat", "postgres", "rabbitmq", "redis"):
        assert f"  {service}:" in text, f"缺少服务 {service}"


def test_compose_pins_image_versions():
    """镜像版本固定,不使用不受控 latest(§二.6/§十三)。"""
    text = COMPOSE.read_text(encoding="utf-8")
    assert "postgres:16-alpine" in text
    assert "rabbitmq:3.13-alpine" in text
    assert "redis:7-alpine" in text
    for line in text.splitlines():
        if line.strip().startswith("image:"):
            assert "latest" not in line, f"镜像不得使用 latest:{line.strip()}"


def test_compose_declares_healthchecks_and_broker_url():
    text = COMPOSE.read_text(encoding="utf-8")
    assert text.count("healthcheck") >= 3, "基础设施服务必须显式健康检查"
    assert text.count("AIVERO_BROKER_URL:") >= 3
    assert "AIVIDEO_BROKER_URL" not in text
    assert "AIVERO_DB_URL" in text
    assert "AIVERO_REDIS_URL" in text


def test_runtime_reads_the_same_broker_env_as_compose():
    """Celery 与 Alembic 必须读取 compose 注入的 AIVERO_ 前缀环境变量。"""
    celery = (ROOT / "apps/api/backend/workers/celery_app.py").read_text(encoding="utf-8")
    env = (ROOT / "apps/api/backend/migrations/env.py").read_text(encoding="utf-8")
    assert 'os.environ.get("AIVERO_BROKER_URL"' in celery
    assert 'os.environ.get("AIVERO_DB_URL"' in env
    assert "AIVIDEO_BROKER_URL" not in celery
