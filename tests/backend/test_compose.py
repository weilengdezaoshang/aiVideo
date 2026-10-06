"""One Compose application stack, with development retaining only a filename alias."""

from pathlib import Path

ROOT = Path(__file__).parent.parent.parent


def test_default_and_dev_compose_resolve_the_same_application_stack():
    default = (ROOT / "docker-compose.yml").read_text()
    dev = (ROOT / "compose.dev.yaml").read_text()
    assert "compose.execution.yaml" in default
    assert "compose.production.yaml" in default
    assert "path: docker-compose.yml" in dev
    assert "services:" not in default and "services:" not in dev


def test_execution_stack_declares_durable_services_and_safe_redis_policy():
    text = (ROOT / "compose.execution.yaml").read_text()
    for service in ("postgres", "rabbitmq", "redis", "migrate", "dispatcher", "scheduler",
                    "bridge", "notify", "image-worker", "video-worker", "export-worker"):
        assert f"  {service}:" in text
    assert "  beat:" not in text
    assert "noeviction" in text and "allkeys-lru" not in text
    assert text.count("healthcheck:") >= 3
    for image in ("postgres:16-alpine", "rabbitmq:3.13-alpine", "redis:7-alpine"):
        assert image in text


def test_api_overlay_has_no_runtime_mode_switch():
    text = (ROOT / "compose.production.yaml").read_text()
    assert "  api:" in text and "  gateway:" in text
    assert "AIVERO_API_MODE" not in text
    assert "AIVERO_PRODUCTION_OIDC_ISSUER" in text
    assert "AIVERO_PRODUCTION_PROVIDER_ROUTES_FILE" in text


def test_runtime_reads_the_same_broker_env_as_compose():
    celery = (ROOT / "apps/api/backend/workers/celery_app.py").read_text()
    env = (ROOT / "apps/api/backend/migrations/env.py").read_text()
    text = (ROOT / "compose.execution.yaml").read_text()
    assert 'os.environ.get("AIVERO_BROKER_URL"' in celery
    assert 'os.environ.get("AIVERO_DB_URL"' in env
    assert "AIVERO_BROKER_URL:" in text
    assert "AIVERO_DB_URL:" in text and "AIVERO_REDIS_URL:" in text
