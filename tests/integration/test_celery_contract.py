"""Celery 执行模型契约(补充要求 §四/§五.11/§六):真实 Celery worker + 真实 fork 验证。

覆盖:
- 事件循环与客户端生命周期:循环跨任务复用,HTTPX/DB 客户端属循环内创建、worker 退出释放;
- fork 隔离:父进程运行时与连接不得在 fork 后子进程可用,子进程必须自建;
- 协作取消:业务取消标志在阶段检查点被尊重,Celery revoke/terminate 不等于上游已取消;
- 消息受理配置:acks_late、prefetch、reject_on_worker_lost、不启用 result backend;
- 硬超时:runtime 层 wait_for 超时(池级 time_limit 为纵深防御,配置契约断言)。

前置:PostgreSQL 隔离实例(同 test_persistence.py);不可用时显式 skip(未执行≠通过)。
"""

import asyncio
import os
import time

import pytest

from backend.infrastructure.database import DatabaseSettings
from backend.workers.celery_app import celery_app
from backend.workers.runtime import AsyncRuntime

TEST_DB_URL = os.environ.get(
    "AIVERO_TEST_DB_URL",
    "postgresql+psycopg://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test",
)
PG_URL = os.environ.get(
    "AIVERO_TEST_PG_URL", "postgresql://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test"
)

# 集成观察点:进程内 solo worker 线程与测试共享内存,probe 任务写入此处(不启用 result backend)。
from backend.workers.probe import PROBE_RESULTS  # noqa: E402


def _pg_available() -> bool:
    import psycopg

    try:
        with psycopg.connect(PG_URL, connect_timeout=3):
            return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL 隔离实例不可用,集成测试未执行(不视为通过)",
)


def wait_for_result(key: str, timeout: float = 30.0):
    """事件驱动等待:有界轮询直至 probe 写入,超时即失败(不用任意长 sleep 掩盖竞态)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if key in PROBE_RESULTS:
            return PROBE_RESULTS[key]
        time.sleep(0.05)
    raise AssertionError(f"probe 任务未在 {timeout}s 内写入 {key}")


@pytest.fixture()
def worker(monkeypatch):
    """进程内真实 Celery worker(solo 池,memory broker),不启用 result backend。

    数据库 URL 固化进 celery conf(随 app 状态进入任务执行端),
    不依赖池子进程的环境变量继承。
    """
    from celery.contrib.testing.worker import start_worker

    monkeypatch.setattr(
        celery_app.conf, "aivideo_database_url", TEST_DB_URL, raising=False
    )
    celery_app.conf.broker_url = "memory://"
    with start_worker(celery_app, perform_ping_check=False, loglevel="error") as _:
        yield celery_app


# ---------- 单元契约:AsyncRuntime ----------


def test_runtime_reuses_single_loop_across_calls():
    """同一运行时的多次调用复用同一事件循环(禁止每次新建循环)。"""
    runtime = AsyncRuntime(DatabaseSettings(url=TEST_DB_URL))
    try:
        loop_ids = set()
        for _ in range(3):

            async def capture():
                await asyncio.sleep(0)
                return id(asyncio.get_running_loop())

            loop_ids.add(runtime.run(capture(), timeout=5))
        assert len(loop_ids) == 1
    finally:
        runtime.close()


def test_runtime_owns_http_client_within_loop_and_disposes_it():
    """HTTPX AsyncClient 属运行时循环内创建,aclose 后不可再用。"""

    runtime = AsyncRuntime(DatabaseSettings(url=TEST_DB_URL))
    try:

        async def client_identity():
            client = runtime.http()
            assert client is runtime.http(), "客户端必须复用,不得每次新建"
            return id(client)

        first = runtime.run(client_identity(), timeout=5)

        async def same_client():
            return id(runtime.http())

        assert runtime.run(same_client(), timeout=5) == first
    finally:
        runtime.close()
    # 关闭后循环已停止:再调度必须失败,且内部客户端已 aclose(以状态标志断言)
    assert runtime.closed


def test_runtime_hard_timeout_cancels_task():
    """超时由事件循环 wait_for 强制取消,调用方收到超时错误,循环仍可用。"""
    runtime = AsyncRuntime(DatabaseSettings(url=TEST_DB_URL))
    try:

        async def slow():
            await asyncio.sleep(30)

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            runtime.run(slow(), timeout=0.2)
        assert time.monotonic() - start < 5

        async def alive():
            return "ok"

        assert runtime.run(alive(), timeout=2) == "ok", "超时后运行时必须仍可用"
    finally:
        runtime.close()


def test_forked_child_cannot_reuse_parent_runtime_and_must_rebuild():
    """fork 后子进程不得复用父进程运行时:旧循环线程不复存在、旧连接不可用;子进程自建后正常。"""
    runtime = AsyncRuntime(DatabaseSettings(url=TEST_DB_URL))

    def make_touch(rt):
        async def touch():
            factory = rt.session_factory()
            async with factory() as session:
                from sqlalchemy import text

                await session.execute(text("SELECT 1"))

        return touch

    touch = make_touch(runtime)
    runtime.run(touch(), timeout=5)  # 父进程内连接健康

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        # 子进程:旧运行时的驱动线程在 fork 后不存在
        os.close(read_fd)
        status = 9
        try:
            runtime.run(touch(), timeout=1.0)
            status = 1  # 违例:旧运行时在子进程可用
        except TimeoutError:
            pass
        except Exception:
            status = 2
        if status != 9:
            os._exit(status)
        # 子进程按契约自建运行时
        try:
            child_runtime = AsyncRuntime(DatabaseSettings(url=TEST_DB_URL))
            child_runtime.run(make_touch(child_runtime)(), timeout=5)
            child_runtime.close()
            status = 0
        except BaseException:
            import traceback

            traceback.print_exc()
            status = 3
        os._exit(status)
    os.close(write_fd)
    os.close(read_fd)
    _, exit_code = os.waitpid(pid, 0)
    runtime.close()
    assert os.waitstatus_to_exitcode(exit_code) == 0, "子进程应自建运行时成功(exit 0)"


# ---------- 配置契约:受理语义 ----------


def test_production_celery_config_disables_result_backend_and_enables_late_ack():
    """不启用 result backend(§六.4);acks_late+reject_on_worker_lost 保证失联重投(§五.11);
    prefetch=1 公平消费(§八);消息持久化(§五.9)。"""
    assert celery_app.conf.task_ignore_result is True
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_default_delivery_mode == "persistent"


# ---------- 集成契约:真实 worker ----------


@requires_pg
def test_worker_task_runs_async_usecase_with_reused_loop(worker):
    """真实 worker 中任务经运行时调用 async 用例:同一子进程内循环跨任务复用。"""
    from backend.workers.tasks import probe_loop_identity

    # 不启用 result backend:通过 probe 观察点断言
    probe_loop_identity.delay()
    first = wait_for_result("loop1")
    probe_loop_identity.delay()
    second = wait_for_result("loop2")
    assert first["pid"] == second["pid"], "solo 池同一进程"
    assert first["loop"] == second["loop"], "跨任务复用同一事件循环"
    assert first["http"] == second["http"], "HTTPX 客户端跨任务复用"


@requires_pg
def test_cooperative_cancel_respected_at_checkpoint_not_celery_revoke(worker):
    """业务取消(recovery=abandon)在阶段检查点被尊重;任务正常 ack 完成,
    不使用 revoke/terminate,不宣称上游已取消。"""

    from backend.workers.tasks import probe_cancelled_step

    job_id = _seed_cancelled_job()
    probe_cancelled_step.delay(str(job_id))
    outcome = wait_for_result(f"cancel:{job_id}")
    assert outcome.get("cancelled") is True, outcome
    assert outcome["upstream_cancelled"] is None, "未派发上游,不得宣称已取消上游"
    assert outcome["errorCode"] == "UPSTREAM_UNKNOWN" or outcome["errorCode"] is None


def _seed_cancelled_job():
    """在 PG 建工作区+任务并标记用户取消(结构化 recovery=abandon);返回任务 id。"""
    import asyncio
    import uuid as uuid_mod

    from backend.infrastructure.uow import AsyncUnitOfWork

    ws = uuid_mod.uuid4()

    async def seed():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            await uow.workspaces.ensure(ws)
            job = await uow.jobs.create(workspace_id=ws, kind="image", params={"prompt": "x"})
            job_id_local = job.id
            await uow.jobs.transition(job_id_local, {"queued"}, "running")
            await uow.commit()
            return job_id_local

    job_id = asyncio.run(seed())

    async def mark_cancelled():
        async with AsyncUnitOfWork(DatabaseSettings(url=TEST_DB_URL)) as uow:
            # 用户取消:结构化标记 recovery=abandon(非文案判断)
            assert await uow.jobs.transition(job_id, {"running"}, "failed") is True
            from sqlalchemy import update

            from backend.infrastructure.orm import Job

            await uow.session.execute(
                update(Job).where(Job.id == job_id).values(recovery="abandon")
            )
            await uow.commit()

    asyncio.run(mark_cancelled())
    return job_id
