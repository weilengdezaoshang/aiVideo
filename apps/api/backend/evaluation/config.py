"""评测子系统配置(pydantic-settings):环境变量单一事实源。

此前 EVAL_*/裁判配置散落在 scheduler.instance 与 make_judge 里手工解析,
非法值要么静默吞掉要么行为不一;统一到 BaseSettings 后:
- 环境变量名自动映射(EVAL_MAX_CONCURRENT_RUNS → max_concurrent_runs);
- 类型/范围校验在读取期完成,配置错误快速失败而非带病运行;
- 测试可直接构造 EvalConfig 注入,不再污染进程环境。
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EvalConfig(BaseSettings):
    """评测环境配置;全部有保守默认值,不设置任何环境变量即可安全运行。"""

    model_config = SettingsConfigDict(env_prefix="EVAL_", extra="ignore")

    max_concurrent_runs: int = Field(default=1, ge=1, le=64)
    # 项目总预算:未设置则不启用项目级账本(None)。
    project_max_cost: Decimal | None = None
    # 项目账本价格快照;generation 未单独提供时回退项目总上限(保守)。
    project_price_generation: Decimal | None = None
    project_price_translate: Decimal = Decimal("0")
    project_price_poll: Decimal = Decimal("0")
    project_price_judge: Decimal = Decimal("0")
    # 抽样策略 JSON(ReviewSamplingPolicy);非法 JSON 在使用处显式报错,不静默回默认。
    review_sampling: str | None = None
    # vlm 裁判(OpenAI 兼容端点);密钥只从环境读取,绝不写入 manifest。
    judge_api_key: str = ""
    judge_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    judge_model: str = "qwen-vl-max-latest"

    @property
    def langfuse_configured(self) -> bool:
        """Langfuse 凭据是否齐备(未齐备时导出必须如实显示未配置,§3.3)。"""
        import os

        return bool(
            os.environ.get("LANGFUSE_PUBLIC_KEY")
            and os.environ.get("LANGFUSE_SECRET_KEY")
            and os.environ.get("LANGFUSE_HOST")
        )


def load_eval_config() -> EvalConfig:
    return EvalConfig()
