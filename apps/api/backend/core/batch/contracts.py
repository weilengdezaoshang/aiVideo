"""Batch limits. Ordinary Plan max_tasks stays on the supervisor settings."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

COMPILER_VERSION = "batch-compile-v1"


class BatchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_BATCH_", frozen=True)
    max_batch_inputs: int = Field(default=100, gt=0)
    max_rule_steps: int = Field(default=8, gt=0)
    max_sizes: int = Field(default=8, gt=0)
    max_expanded_tasks: int = Field(default=1000, gt=0)
    max_edge_px: int = Field(default=8192, gt=0)
    max_per_batch: int = Field(default=4, gt=0)
    max_per_run: int = Field(default=4, gt=0)
    global_concurrency: int = Field(default=8, gt=0)
    recover_limit: int = Field(default=32, gt=0)
    max_rule_bytes: int = Field(default=65536, gt=0)
