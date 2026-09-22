"""Worker 异步执行环境(补充要求 §四,决策记录 ADR-0003)。

契约:
- 每个 Celery 进程(尤其 prefork fork 后的子进程)自建一个运行时:
  专属守护线程驱动独立事件循环,循环跨任务复用,进程退出时释放;
- HTTPX AsyncClient 与 SQLAlchemy 异步引擎属运行时所有,首次使用时在循环线程内创建,
  close() 时在循环内 aclose/dispose —— 禁止复用绑定旧循环的客户端(§四禁止项);
- run(coro, timeout) 提供硬超时:超时取消协程,循环不受影响(池级
  task_time_limit 是纵深防御,见 celery_app 配置);
- 禁止在循环内执行同步阻塞的数据库/网络操作:任务入口只经 run() 调度 async 用例。
"""

import asyncio
import threading

import httpx
from redis.asyncio import Redis
import os
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.infrastructure.database import (
    DatabaseSettings,
    create_async_database_engine,
    create_async_session_factory,
)


class AsyncRuntime:
    """每个 Celery 进程一个异步运行时:专属线程事件循环 + 循环内创建的客户端。"""

    def __init__(self, settings: DatabaseSettings | None = None):
        self._settings = settings or DatabaseSettings()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._drive, name="aivideo-async-runtime", daemon=True
        )
        self._ready = threading.Event()
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker | None = None
        self._http: httpx.AsyncClient | None = None
        self._artifact_http: httpx.AsyncClient | None = None
        self._redis: Redis | None = None
        self._closed = False
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("异步运行时线程启动超时")

    def _drive(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def _assert_loop_thread(self) -> None:
        if threading.current_thread() is not self._thread:
            raise RuntimeError("客户端只能在运行时循环线程内创建与使用")

    def run(self, coro, timeout: float | None = None):
        """在运行时循环上执行协程;timeout 为硬超时(取消协程,循环保持可用)。"""
        if self._closed or not self._loop.is_running():
            raise RuntimeError("异步运行时已关闭")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()  # 协作取消:循环侧取消任务,不等待阻塞操作
            raise TimeoutError("异步用例执行超时") from None

    def http(self) -> httpx.AsyncClient:
        """进程内共享 HTTPX 客户端;仅在循环线程内首次创建(随循环生命周期)。"""
        self._assert_loop_thread()
        if self._closed:
            raise RuntimeError("异步运行时已关闭")
        if self._http is None:
            from backend.providers.policy import ProviderPolicy
            self._http = httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=ProviderPolicy().http_timeout(30),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
        return self._http

    def artifact_http(self) -> httpx.AsyncClient:
        self._assert_loop_thread()
        if self._artifact_http is None:
            from backend.infrastructure.public_http import create_public_client
            self._artifact_http = create_public_client()
        return self._artifact_http

    def redis(self) -> Redis:
        self._assert_loop_thread()
        if self._redis is None:
            self._redis = Redis.from_url(os.environ["AIVERO_REDIS_URL"],
                                         socket_connect_timeout=2, socket_timeout=2)
        return self._redis

    def session_factory(self) -> async_sessionmaker:
        """进程内共享异步引擎与会话工厂;Session 每事务创建,不跨并发任务共享(§三.3)。"""
        self._assert_loop_thread()
        if self._closed:
            raise RuntimeError("异步运行时已关闭")
        if self._session_factory is None:
            self._engine = create_async_database_engine(self._settings)
            self._session_factory = create_async_session_factory(self._engine)
        return self._session_factory

    async def aclose(self) -> None:
        if self._artifact_http is not None:
            await self._artifact_http.aclose()
            self._artifact_http = None
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """释放客户端并停止循环;幂等。"""
        if self._closed:
            return
        if self._loop.is_running():
            try:
                self.run(self.aclose(), timeout=15)
            except Exception:
                pass  # 退出路径尽力释放;引擎进程随进程回收
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


_runtime: AsyncRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime(settings: DatabaseSettings | None = None) -> AsyncRuntime:
    """进程级单例;首次调用创建(fork 后子进程的首次任务必然发生在 fork 之后)。"""
    global _runtime
    with _runtime_lock:
        if _runtime is None or _runtime.closed:
            _runtime = AsyncRuntime(settings)
        return _runtime


def peek_runtime() -> AsyncRuntime | None:
    return _runtime
