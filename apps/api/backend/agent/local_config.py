"""Development-only workspace dialogue settings, never exposed in run snapshots."""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from backend.agent.config import AgentRoute
from backend.services.identity import Identity, authenticated


class DialogueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    baseUrl: str
    model: str = Field(min_length=1, max_length=128)
    timeoutMs: int = Field(default=45000, ge=30000, le=60000, strict=True)
    apiKey: SecretStr | None = Field(default=None)

    @field_validator("baseUrl")
    @classmethod
    def endpoint(cls, value: str) -> str:
        return AgentRoute.validate_base_url(value.strip())

    @field_validator("model")
    @classmethod
    def model_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Model name is required")
        return value.strip()

    @field_validator("apiKey")
    @classmethod
    def key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            key = value.get_secret_value().strip()
            if not key or len(key) > 4096 or any(char in key for char in "\r\n"):
                raise ValueError("Invalid key")
            return SecretStr(key)
        return value


def config_path(root: Path, workspace: uuid.UUID) -> Path:
    return root / "private-agent-config" / f"{workspace}.json"


def load_config(root: Path, workspace: uuid.UUID) -> DialogueConfig | None:
    path = config_path(root, workspace)
    return DialogueConfig.model_validate_json(path.read_text()) if path.exists() else None


def public_status(config: DialogueConfig | None) -> dict:
    return {"configured": config is not None, "keyConfigured": bool(config and config.apiKey),
            "baseUrl": config.baseUrl if config else "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": config.model if config else "qwen-plus", "timeoutMs": config.timeoutMs if config else 45000}


def save_config(root: Path, workspace: uuid.UUID, raw: object) -> DialogueConfig:
    config = DialogueConfig.model_validate(raw)
    previous = load_config(root, workspace)
    if config.apiKey is None:
        if previous is None or previous.apiKey is None:
            raise ValueError("API Key required")
        config = config.model_copy(update={"apiKey": previous.apiKey})
    path = config_path(root, workspace)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {**config.model_dump(exclude={"apiKey"}), "apiKey": config.apiKey.get_secret_value()}
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return config


def dialogue_route(root: Path, workspace: uuid.UUID) -> AgentRoute | None:
    config = load_config(root, workspace)
    if config is None or config.apiKey is None:
        return None
    return AgentRoute(id="local-dialogue", label=config.model, provider="bailian", model=config.model,
                      baseUrl=config.baseUrl, credentialRef=f"local-dialogue:{workspace}",
                      timeoutSeconds=config.timeoutMs / 1000)


def install_local_config_routes(app, read_json):
    @app.api_route("/api/creation/v1/llm-config", methods=["GET", "POST"])
    async def configuration(request: Request, identity: Identity = Depends(authenticated)):
        if app.state.settings.runtime_environment != "development":
            raise HTTPException(403, "此部署未开放本机对话模型配置")
        if identity.role not in {"owner", "admin"}:
            raise HTTPException(403, "仅工作区管理员可管理 API 配置")
        try:
            config = (save_config(app.state.settings.media_root, identity.workspace_id, await read_json(request))
                      if request.method == "POST" else load_config(app.state.settings.media_root, identity.workspace_id))
        except (ValidationError, ValueError):
            raise HTTPException(400, "配置无效，请检查 HTTPS 接口地址、模型、Key 和超时") from None
        except OSError:
            raise HTTPException(503, "配置读写失败，请检查服务端文件权限") from None
        return {**public_status(config), "hostEnabled": app.state.host.catalog.config.enabled}
