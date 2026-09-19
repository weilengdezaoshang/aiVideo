"""Unit of Work(补充要求 §三):显式事务上下文。

- 进入时创建新 Session(不跨并发任务共享),正常退出 commit、异常退出 rollback,
  退出后关闭;Repository 不自行 commit。
- 任务、幂等记录与 Outbox 因此天然处于同一事务(§五.1/§三.6)。
- 构造来源三选一:session_factory(生产:进程级共享引擎)、DatabaseSettings
  (独立引擎,UoW 退出时 dispose;测试/单用例场景)、None(进程默认配置)。
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.infrastructure.database import (
    DatabaseSettings,
    create_async_database_engine,
    create_async_session_factory,
)
from backend.infrastructure.repositories import (
    AssetRepository,
    GenerationRequestRepository,
    JobRepository,
    OutboxRepository,
    WorkspaceRepository,
)


class AsyncUnitOfWork:
    def __init__(
        self,
        source: async_sessionmaker[AsyncSession] | DatabaseSettings | None = None,
    ):
        self._source = source
        self._owned_engine = None
        self.session: AsyncSession | None = None
        self.jobs: JobRepository | None = None
        self.requests: GenerationRequestRepository | None = None
        self.outbox: OutboxRepository | None = None
        self.workspaces: WorkspaceRepository | None = None
        self.assets: AssetRepository | None = None

    def _resolve_factory(self) -> async_sessionmaker[AsyncSession]:
        if isinstance(self._source, async_sessionmaker):
            return self._source
        settings = self._source or DatabaseSettings()
        # DatabaseSettings 路径按实例创建引擎并在退出时释放(§三.1:按进程生命周期管理)。
        self._owned_engine = create_async_database_engine(settings)
        return create_async_session_factory(self._owned_engine)

    async def __aenter__(self) -> "AsyncUnitOfWork":
        factory = self._resolve_factory()
        self.session = factory()
        self.jobs = JobRepository(self.session)
        self.requests = GenerationRequestRepository(self.session)
        self.outbox = OutboxRepository(self.session)
        self.workspaces = WorkspaceRepository(self.session)
        self.assets = AssetRepository(self.session)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                await self.commit()
            else:
                await self.rollback()
        finally:
            assert self.session is not None
            await self.session.close()
            self.session = None
            self.jobs = self.requests = self.outbox = self.workspaces = self.assets = None
            if self._owned_engine is not None:
                await self._owned_engine.dispose()
                self._owned_engine = None

    async def commit(self) -> None:
        assert self.session is not None
        await self.session.commit()

    async def rollback(self) -> None:
        assert self.session is not None
        await self.session.rollback()
