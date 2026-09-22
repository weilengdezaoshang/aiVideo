"""Redis 任务状态缓存契约(补充要求 §十):真实 Redis 7 隔离实例验证。

覆盖:热点命中减少 DB 读取、并发未命中合并回源、旧回填/乱序事件不覆盖新状态、
Redis 清空后旧事件不恢复过期状态、Redis 不可用降级且回源限并发、跨工作空间拒绝。

前置:docker run -d --name aivideo-test-redis --network aivideo-test \
  redis:7-alpine --maxmemory 64mb --maxmemory-policy allkeys-lru
"""

import asyncio
import os
import socket
import threading
import uuid

import pytest

TEST_DB_URL = os.environ.get(
    "AIVERO_TEST_DB_URL",
    "postgresql+psycopg://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test",
)
PG_URL = os.environ.get(
    "AIVERO_TEST_PG_URL", "postgresql://aivideo:aivideo-test@127.0.0.1:55433/aivideo_test"
)
REDIS_URL = os.environ.get("AIVERO_TEST_REDIS_URL", "redis://127.0.0.1:56379/0")
REDIS_HOST = os.environ.get("AIVERO_TEST_REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("AIVERO_TEST_REDIS_PORT", "56379"))


def _redis_available() -> bool:
    try:
        with socket.create_connection((REDIS_HOST, REDIS_PORT), timeout=2):
            return True
    except OSError:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_available(),
    reason="Redis 隔离实例不可用,集成测试未执行(不视为通过)",
)


def make_snapshot(**overrides) -> dict:
    base = {
        "jobId": str(uuid.uuid4()),
        "workspaceId": str(uuid.uuid4()),
        "status": "running",
        "messageCode": None,
        "recovery": None,
        "stateVersion": 3,
        "executionEpoch": 1,
        "progressSeq": 0,
        "progress": 0.5,
        "updatedAt": "2026-09-13T12:00:00+00:00",
        "assetIds": [],
    }
    base.update(overrides)
    return base


@pytest.fixture()
def redis_url():
    return REDIS_URL


def make_cache(redis_url) -> "tuple[object, object]":
    import redis

    from backend.infrastructure.job_cache import JobCache, JobCacheSettings

    client = redis.Redis.from_url(redis_url, decode_responses=True)
    settings = JobCacheSettings(ttl_s=120, terminal_ttl_s=600, key_prefix="aivideo-test")
    return JobCache(client, settings), client


# ---------- 命中与回源 ----------


@requires_redis
def test_cache_hit_reduces_database_reads(redis_url):
    """命中缓存时不读数据库;缓存内容与投影一致。"""
    cache, _ = make_cache(redis_url)
    snapshot = make_snapshot()
    cache.store_snapshot(snapshot)

    reads = {"n": 0}

    def db_reader(workspace_id, job_id):
        reads["n"] += 1
        return snapshot

    from backend.infrastructure.job_cache import SnapshotStore

    store = SnapshotStore(cache, db_reader)
    for _ in range(5):
        result = store.get(snapshot["workspaceId"], snapshot["jobId"])
        assert result["status"] == "running"
    assert reads["n"] == 0, "命中缓存不得触发数据库读取"


@requires_redis
def test_concurrent_misses_merge_into_single_read(redis_url):
    """并发未命中:同键回源合并(singleflight),数据库只读一次(§十.11)。"""
    cache, _ = make_cache(redis_url)
    snapshot = make_snapshot()
    reads = {"n": 0}
    lock = threading.Lock()

    def db_reader(workspace_id, job_id):
        with lock:
            reads["n"] += 1
        return snapshot

    from backend.infrastructure.job_cache import SnapshotStore

    store = SnapshotStore(cache, db_reader)

    async def burst():
        store_async = store

        async def one():
            return await asyncio.to_thread(
                store_async.get, snapshot["workspaceId"], snapshot["jobId"]
            )

        return await asyncio.gather(*[one() for _ in range(20)])

    results = asyncio.run(burst())
    assert all(r["jobId"] == snapshot["jobId"] for r in results)
    assert reads["n"] == 1, f"20 并发未命中应合并为一次回源,实际 {reads['n']}"


# ---------- 状态版本竞争 ----------


@requires_redis
def test_stale_backfill_cannot_overwrite_newer_state(redis_url):
    """旧数据库查询结果回填不得覆盖较新的缓存状态(§十.4/§十.5)。"""
    cache, _ = make_cache(redis_url)
    newer = make_snapshot(status="completed", stateVersion=5, progress=1.0)
    cache.store_snapshot(newer)

    stale = make_snapshot(status="running", stateVersion=3, progress=0.5)
    cache.store_snapshot(stale)  # 旧回填

    result = cache.get_snapshot(newer["workspaceId"], newer["jobId"])
    assert result["status"] == "completed", "旧回填不得覆盖新状态"


@requires_redis
def test_out_of_order_events_rejected(redis_url):
    """乱序事件与旧执行代次的进度不得覆盖新状态(§十.9)。"""
    cache, _ = make_cache(redis_url)
    base = make_snapshot(stateVersion=4, executionEpoch=2, progressSeq=10, progress=0.8)
    cache.store_snapshot(base)

    stale_seq = dict(base, progressSeq=8, progress=0.3, stateVersion=4)
    assert cache.apply_event(stale_seq) is False, "进度序号倒退必须拒绝"

    stale_epoch = dict(base, executionEpoch=1, progressSeq=99, progress=0.9)
    assert cache.apply_event(stale_epoch) is False, "旧执行代次必须拒绝"

    fresh = dict(base, progressSeq=11, progress=0.9, stateVersion=4)
    assert cache.apply_event(fresh) is True
    result = cache.get_snapshot(base["workspaceId"], base["jobId"])
    assert result["progress"] == 0.9


@requires_redis
def test_redis_flush_then_stale_event_must_not_revive(redis_url):
    """键被淘汰/Redis 清空后,迟到旧事件不得凭空恢复状态(§十.6):必须走数据库重新投影。"""
    cache, client = make_cache(redis_url)
    snapshot = make_snapshot(stateVersion=7, status="completed")
    cache.store_snapshot(snapshot)
    client.flushdb()

    late_event = dict(snapshot, status="running", stateVersion=6)
    assert cache.apply_event(late_event) is False, "键缺失时事件不得凭空写入"

    assert cache.get_snapshot(snapshot["workspaceId"], snapshot["jobId"]) is None
    # 正确恢复路径:数据库重新投影(条件回填)
    cache.store_snapshot(snapshot)
    result = cache.get_snapshot(snapshot["workspaceId"], snapshot["jobId"])
    assert result["status"] == "completed" and result["stateVersion"] == 7


# ---------- 降级与回源限流 ----------


@requires_redis
def test_redis_unavailable_degrades_and_limits_backfill_concurrency(redis_url, monkeypatch):
    """Redis 不可用:降级读数据库;并发回源受信号量限制(§十.11)。"""
    from backend.infrastructure.job_cache import JobCacheSettings, SnapshotStore

    dead_url = "redis://127.0.0.1:59999/0"
    import redis

    from backend.infrastructure.job_cache import JobCache

    client = redis.Redis.from_url(dead_url, decode_responses=True, socket_connect_timeout=0.2)
    settings = JobCacheSettings(ttl_s=60, terminal_ttl_s=300, key_prefix="aivideo-test")
    cache = JobCache(client, settings)

    snapshot = make_snapshot()
    state = {"current": 0, "peak": 0}
    lock = threading.Lock()

    def db_reader(workspace_id, job_id):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        import time as _t

        _t.sleep(0.05)
        with lock:
            state["current"] -= 1
        return snapshot

    store = SnapshotStore(cache, db_reader, max_backfill_concurrency=3)

    async def burst():
        async def one():
            return await asyncio.to_thread(store.get, snapshot["workspaceId"], snapshot["jobId"])

        return await asyncio.gather(*[one() for _ in range(12)])

    results = asyncio.run(burst())
    assert all(r["status"] == "running" for r in results), "降级必须仍返回数据库结果"
    assert state["peak"] <= 3, f"回源并发峰值 {state['peak']} 超过限制 3"


# ---------- 安全与一致性 ----------


@requires_redis
def test_cross_workspace_access_denied(redis_url):
    """workspace key 前缀不能代替鉴权:跨工作空间读取必须拒绝(§十.13)。"""
    cache, _ = make_cache(redis_url)
    snapshot = make_snapshot()
    cache.store_snapshot(snapshot)

    other_workspace = str(uuid.uuid4())
    assert (
        cache.get_snapshot(other_workspace, snapshot["jobId"]) is None
    ), "跨工作空间读取必须拒绝"


@requires_redis
def test_cache_and_database_eventually_consistent(redis_url):
    """缓存与数据库状态最终恢复一致:投影→缓存;乱序覆盖后由条件回填纠正。"""
    cache, _ = make_cache(redis_url)
    from backend.infrastructure.job_cache import SnapshotStore

    db_state = {"version": 1}
    ws, jid = str(uuid.uuid4()), str(uuid.uuid4())

    def db_reader(workspace_id, job_id):
        assert workspace_id == ws and job_id == jid
        if db_state["version"] == 1:
            return make_snapshot(
                jobId=jid, workspaceId=ws, status="running", stateVersion=1, progress=0.2
            )
        return make_snapshot(
            jobId=jid,
            workspaceId=ws,
            status="completed",
            stateVersion=2,
            progress=1.0,
            assetIds=["a-1"],
        )

    store = SnapshotStore(cache, db_reader)
    first = store.get(ws, jid)
    assert first["status"] == "running"

    # 数据库推进到终态,事件按同一任务更新缓存
    db_state["version"] = 2
    terminal = make_snapshot(
        jobId=jid,
        workspaceId=ws,
        status="completed",
        stateVersion=2,
        progress=1.0,
        assetIds=["a-1"],
    )
    assert cache.apply_event(terminal) is True
    result = store.get(ws, jid)
    assert result["status"] == "completed" and result["assetIds"] == ["a-1"]
