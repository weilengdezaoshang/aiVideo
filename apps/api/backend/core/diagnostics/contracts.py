from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_DIAGNOSTICS_", frozen=True)
    page_size: int = Field(default=200, gt=0, le=1000)
    window_seconds: float = Field(default=3600, gt=0)
    min_samples: int = Field(default=10, gt=0)
    run_failure_rate: float = Field(default=0.2, ge=0, le=1)
    tool_failure_rate: float = Field(default=0.3, ge=0, le=1)
    review_rejection_rate: float = Field(default=0.5, ge=0, le=1)
    refund_credits: int = Field(default=1000, ge=0)
    backlog_count: int = Field(default=1000, gt=0)
    max_tool_labels: int = Field(default=128, gt=0)
    poll_seconds: float = Field(default=30, gt=0)
