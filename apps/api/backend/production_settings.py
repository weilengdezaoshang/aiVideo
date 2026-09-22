"""Explicit production configuration. Never loads legacy config.json or dev identity."""
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProductionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_PRODUCTION_", frozen=True, hide_input_in_errors=True)

    public_origin: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: SecretStr
    session_secret: SecretStr
    provider_routes_file: Path
    media_root: Path
    metrics_token: SecretStr | None = None
    session_lifetime_s: int = Field(default=28800, ge=300, le=86400)
    sse_refresh_s: float = Field(default=5, ge=1, le=30)

    @model_validator(mode="after")
    def validate_secure_settings(self):
        for value in (self.public_origin, self.oidc_issuer):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
                raise ValueError("Production origin and OIDC issuer require HTTPS without credentials/query")
        if urlsplit(self.public_origin).path not in {"", "/"} or self.public_origin.endswith("/"):
            raise ValueError("public_origin must be an origin without a trailing slash")
        if len(self.session_secret.get_secret_value()) < 32:
            raise ValueError("session_secret requires at least 32 characters")
        if not self.oidc_client_id or not self.oidc_client_secret.get_secret_value():
            raise ValueError("OIDC client credentials are required")
        return self
