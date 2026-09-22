"""任务状态缓存(补充要求 §十):高频读取走 Redis,状态所有权仍以 PostgreSQL 为准。

- 安全快照字段白名单:jobId/workspaceId/status/messageCode/recovery/stateVersion/
  executionEpoch/progressSeq/progress/updatedAt/assetIds;
  密钥、原始异常、媒体二进制、完整敏感参数不入缓存(§十.不缓存项);
- 写入与事件更新都按 stateVersion/executionEpoch/progressSeq 条件比较(§十.4/5/9):
  旧回填、乱序事件、旧执行代次一律拒绝;键缺失时事件不得凭空恢复(§十.6),
  必须由数据库重新投影;
- 查询入口的鉴权在调用方完成;缓存层额外校验 workspaceId 一致(§十.13,
  键前缀不能代替鉴权);
- 缓存键前缀(aivideo:cache:)与限流/协调数据(其他前缀)隔离(§十.14),
  淘汰策略只影响缓存前缀(部署配置 maxmemory-policy);
- 降级:Redis 不可用或超时时返回未命中,由 SnapshotStore 限并发回源数据库
  (§十.11),不把缓存故障放大为数据库压垮。
"""

import json
import threading

import redis

CACHE_KEY_PREFIX = "aivideo:cache:job"
SNAPSHOT_FIELDS = (
    "jobId",
    "workspaceId",
    "status",
    "messageCode",
    "recovery",
    "stateVersion",
    "executionEpoch",
    "progressSeq",
    "progress",
    "updatedAt",
    "assetIds",
)

# 条件写入:仅当(新 stateVersion, 新 progressSeq)不小于现有值时覆盖;
# 键不存在时:allow_create(数据库回填)允许创建;事件路径返回 missing,不得凭空写(§十.6)。
# 注意:Redis Lua 中 HMGET 缺失字段是 false,tonumber(false)=nil,nil~=false,须先判存在。
_STORE_LUA = """
local cur = redis.call('HMGET', KEYS[1], 'stateVersion', 'progressSeq', 'executionEpoch')
local pairs_count = tonumber(ARGV[4]) / 2
local allow_create = ARGV[#ARGV] == '1'
local function write_hash()
  redis.call('DEL', KEYS[1])
  for i = 1, pairs_count do
    redis.call('HSET', KEYS[1], ARGV[i * 2 + 3], ARGV[i * 2 + 4])
  end
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[#ARGV - 1]))
end
if cur[1] == false or cur[1] == nil then
  if allow_create then
    write_hash()
    return 'ok'
  end
  return 'missing'
end
local old_ver = tonumber(cur[1])
local old_seq = tonumber(cur[2]) or -1
local old_epoch = tonumber(cur[3]) or 0
local new_ver = tonumber(ARGV[1])
local new_seq = tonumber(ARGV[2])
local new_epoch = tonumber(ARGV[3])
if new_epoch < old_epoch then
  return 'stale'
end
if new_ver < old_ver or (new_ver == old_ver and new_seq < old_seq) then
  return 'stale'
end
write_hash()
return 'ok'
"""


def _clean(snapshot: dict) -> dict:
    return {k: snapshot.get(k) for k in SNAPSHOT_FIELDS}


class JobCacheSettings:
    def __init__(self, *, ttl_s: int = 300, terminal_ttl_s: int = 3600, key_prefix: str = "aivideo"):
        self.ttl_s = ttl_s
        self.terminal_ttl_s = terminal_ttl_s
        self.key_prefix = key_prefix


class JobCache:
    """Redis 哈希存储的安全快照;所有写入走条件比较。"""

    def __init__(self, client: "redis.Redis", settings: JobCacheSettings | None = None):
        self._client = client
        self._settings = settings or JobCacheSettings()
        self._lock = threading.Lock()

    def _key(self, workspace_id, job_id) -> str:
        return f"{CACHE_KEY_PREFIX}:{self._settings.key_prefix}:{workspace_id}:{job_id}"

    def get_snapshot(self, workspace_id, job_id) -> dict | None:
        """命中返回安全快照;跨工作空间读取拒绝(§十.13);故障降级为未命中。"""
        try:
            key = self._key(workspace_id, job_id)
            data = self._client.hgetall(key)
            if not data:
                return None
            if data.get("workspaceId") != str(workspace_id):
                return None  # 前缀不能代替鉴权:内容归属校验
            snapshot = {k: _decode_value(k, v) for k, v in data.items()}
            return snapshot
        except redis.RedisError:
            return None  # §十.11:缓存故障降级,不视为任务不存在(§十.1)

    def store_snapshot(self, snapshot: dict, *, allow_create: bool = True) -> str:
        """条件写入:返回 ok/stale/missing/unavailable。

        allow_create=True 用于数据库投影回填(可信来源,可创建键);
        apply_event 路径 allow_create=False:键缺失时事件不得凭空恢复状态(§十.6)。
        """
        clean = _clean(snapshot)
        try:
            key = self._key(clean["workspaceId"], clean["jobId"])
            args = [
                str(clean["stateVersion"]),
                str(clean["progressSeq"]),
                str(clean["executionEpoch"]),
                str(len(clean) * 2),
            ]
            for k, v in clean.items():
                args.extend([k, _encode_value(v)])
            args.append(str(self._ttl_for(clean)))
            args.append("1" if allow_create else "0")
            script = self._client.register_script(_STORE_LUA)
            with self._lock:
                verdict = script(keys=[key], args=args)
            return verdict.decode() if isinstance(verdict, bytes) else str(verdict)
        except redis.RedisError:
            return "unavailable"

    def apply_event(self, event: dict) -> bool:
        """事件驱动更新:乱序/旧代次/键缺失一律拒绝(必须走数据库重新投影)。"""
        return self.store_snapshot(event, allow_create=False) == "ok"

    def _ttl_for(self, snapshot: dict) -> int:
        return (
            self._settings.terminal_ttl_s
            if snapshot.get("status") in {"completed", "failed", "unknown"}
            else self._settings.ttl_s
        )

    def flush_all(self):
        self._client.flushdb()


def _encode_value(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return json.dumps(value)
    return str(value)


def _decode_value(key: str, value: str):
    if value == "":
        return [] if key == "assetIds" else None
    if key in {"stateVersion", "executionEpoch", "progressSeq"}:
        return int(value)
    if key == "progress":
        return float(value)
    if key == "assetIds":
        return json.loads(value)
    return value


class SnapshotStore:
    """组合读取:缓存命中 → 未命中限并发回源数据库 → 条件回填(§十.查询路径)。"""

    def __init__(self, cache: JobCache, db_reader, max_backfill_concurrency: int = 4):
        self._cache = cache
        self._db_reader = db_reader
        self._semaphore = threading.Semaphore(max_backfill_concurrency)
        self._key_locks_guard = threading.Lock()
        self._key_locks: dict[tuple, threading.Lock] = {}

    def _key_lock(self, workspace_id, job_id) -> threading.Lock:
        with self._key_locks_guard:
            key = (str(workspace_id), str(job_id))
            if key not in self._key_locks:
                self._key_locks[key] = threading.Lock()
            return self._key_locks[key]

    def get(self, workspace_id, job_id) -> dict | None:
        hit = self._cache.get_snapshot(workspace_id, job_id)
        if hit is not None:
            return hit
        # singleflight:同键并发未命中合并为一次回源(§十.11);跨键由信号量限总并发
        with self._key_lock(workspace_id, job_id):
            hit = self._cache.get_snapshot(workspace_id, job_id)
            if hit is not None:
                return hit
            with self._semaphore:
                snapshot = self._db_reader(workspace_id, job_id)
            if snapshot is None:
                return None  # 缓存未命中不等于任务不存在,但数据库确认缺失才算缺失
            self._cache.store_snapshot(snapshot)
            return snapshot


def apply_event_with_backfill(cache: JobCache, event: dict, db_reader) -> bool:
    """生产接线的事件应用(§十.3/§十.6):事件应用失败(键缺失/版本落后)时,
    以数据库重新投影回填 —— 保证迟到旧事件无法凭空恢复,权威状态始终来自数据库。

    db_reader(workspaceId, jobId) -> 快照 | None,由调用方注入(含鉴权与投影)。
    """
    if cache.apply_event(event) is True:
        return True
    snapshot = db_reader(event.get("workspaceId"), event.get("jobId"))
    if snapshot is None:
        return False
    return cache.store_snapshot(snapshot) == "ok"
