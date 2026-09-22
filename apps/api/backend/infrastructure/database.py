"""数据库连接生命周期(补充要求 §三.1/§三.8)。

- Engine/Session 工厂按进程创建:API 进程用异步引擎,Celery worker 在
  worker_process_init 钩子中另行创建同步引擎(禁止复用父进程池,fork 前创建的
  连接不得进入子进程,见 ADR-0003)。
- AsyncSession 不跨并发任务共享:每事务一个 Session(Unit of Work)。
- 连接池预算:API 与 Worker 的 pool_size/max_overflow 分别配置;总预算 =
  Σ(实例数 × 实例池上限),当前部署规模未定,预算值标记待确认(ADR-0002)。
- 超时区分(§三.9):connect_timeout 属驱动参数;pool 等待与语句超时按下述字段
  注入,业务任务超时由任务层负责——四者不混用。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


class DatabaseSettings(BaseSettings):
    """类型化数据库配置(pydantic-settings);env 前缀 AIVERO_DB_。"""

    model_config = SettingsConfigDict(env_prefix="AIVERO_DB_", frozen=True)

    url: str = "postgresql+psycopg://aivideo:aivideo@127.0.0.1:5432/aivideo"
    pool_size: int = 5
    max_overflow: int = 5
    connect_timeout_s: int = 10
    pool_timeout_s: int = 30
    statement_timeout_ms: int = 30_000


@lru_cache(maxsize=1)
def _cached_settings() -> DatabaseSettings:
    return DatabaseSettings()


def create_async_database_engine(settings: DatabaseSettings | None = None) -> AsyncEngine:
    """按调用进程创建异步引擎;调用方负责在进程退出时 dispose。"""
    settings = settings or _cached_settings()
    return create_async_engine(
        settings.url,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_s,
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.connect_timeout_s,
                      "options": f"-c statement_timeout={int(settings.statement_timeout_ms)}"},
    )


def create_async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
