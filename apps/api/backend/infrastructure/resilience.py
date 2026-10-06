"""Atomic Redis coordination. Every mutation is fenced by circuit generation/probe token."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

from backend.providers.policy import ProviderPolicy


ACQUIRE = """
local tm=redis.call('TIME'); local now=tonumber(tm[1])+tonumber(tm[2])/1000000
local key=KEYS[1]
if redis.call('HGET',key,'auth')=='1' then return {'auth','0'} end
local until_at=tonumber(redis.call('HGET',key,'until') or '0')
if until_at>now then return {'open',tostring(until_at-now)} end
local state=redis.call('HGET',key,'state') or 'closed'
if state=='open' or state=='half' then
  local probe_until=tonumber(redis.call('HGET',key,'probe_until') or '0')
  if state=='half' and probe_until>now then return {'open',tostring(probe_until-now)} end
  local gen=redis.call('HINCRBY',key,'generation',1)
  redis.call('HSET',key,'state','half','probe',ARGV[1],'probe_until',now+tonumber(ARGV[2]))
  return {'probe',tostring(gen)}
end
local recovered=tonumber(redis.call('HGET',key,'recovered_at') or '0')
if recovered>0 and now-recovered<tonumber(ARGV[3]) then
 local second=math.floor(now)
 if tonumber(redis.call('HGET',key,'ramp_second') or '0')~=second then
  redis.call('HSET',key,'ramp_second',second,'ramp_calls',0)
 end
 local allowed=1+math.floor((now-recovered)/5)
 if tonumber(redis.call('HGET',key,'ramp_calls') or '0')>=allowed then return {'open','1'} end
 redis.call('HINCRBY',key,'ramp_calls',1)
end
return {'closed',redis.call('HGET',key,'generation') or '0'}
"""

RECORD = """
local key=KEYS[1]
local gen=redis.call('HGET',key,'generation') or '0'
if gen~=ARGV[1] then return 0 end
local tm=redis.call('TIME'); local now=tonumber(tm[1])+tonumber(tm[2])/1000000
local state=redis.call('HGET',key,'state') or 'closed'
if state=='half' and redis.call('HGET',key,'probe')~=ARGV[2] then return 0 end
local outcome=ARGV[3]
if outcome=='auth' then redis.call('HSET',key,'auth','1'); return 1 end
if outcome=='rate' then
 redis.call('HSET',key,'state','open','until',now+tonumber(ARGV[8]))
 redis.call('HINCRBY',key,'generation',1); return 1
end
if state=='half' then
 if outcome=='success' then
  redis.call('HSET',key,'state','closed','recovered_at',now); redis.call('DEL',KEYS[2],KEYS[3])
 else redis.call('HSET',key,'state','open','until',now+tonumber(ARGV[7])) end
 redis.call('HDEL',key,'probe','probe_until'); redis.call('HINCRBY',key,'generation',1)
 return 1
end
if outcome=='ignore' then return 1 end
redis.call('ZREMRANGEBYSCORE',KEYS[2],'-inf',now-tonumber(ARGV[4]))
redis.call('ZREMRANGEBYSCORE',KEYS[3],'-inf',now-tonumber(ARGV[4]))
redis.call('ZADD',KEYS[2],now,ARGV[2])
if outcome=='failure' then redis.call('ZADD',KEYS[3],now,ARGV[2]) end
redis.call('EXPIRE',KEYS[2],tonumber(ARGV[4])*2); redis.call('EXPIRE',KEYS[3],tonumber(ARGV[4])*2)
local count=redis.call('ZCARD',KEYS[2]); local failed=redis.call('ZCARD',KEYS[3])
if count>=tonumber(ARGV[5]) and failed/count>=tonumber(ARGV[6]) then
 redis.call('HSET',key,'state','open','until',now+tonumber(ARGV[7]))
 redis.call('HINCRBY',key,'generation',1)
end
return 1
"""

RATE = """
local tm=redis.call('TIME'); local now=tonumber(tm[1])+tonumber(tm[2])/1000000
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now-tonumber(ARGV[1]))
if redis.call('ZCARD',KEYS[1])>=tonumber(ARGV[2]) then return 0 end
redis.call('ZADD',KEYS[1],now,ARGV[3]); redis.call('EXPIRE',KEYS[1],tonumber(ARGV[1])+1)
return 1
"""


class CircuitDeferred(Exception):
    def __init__(self, seconds: float, reason: str = "open"):
        self.seconds, self.reason = max(1, seconds), reason
        super().__init__("Provider temporarily paused")


@dataclass(frozen=True)
class Permit:
    scope: str
    token: str
    generation: str


def scope_key(endpoint: str, credential_version: str, model: str, operation: str) -> str:
    # Include a credential reference/version, never the secret itself.
    return hashlib.sha256("\0".join((endpoint, credential_version, model, operation)).encode()).hexdigest()


class RedisResilience:
    def __init__(self, client: Redis, policy: ProviderPolicy | None = None):
        self.client, self.policy = client, policy or ProviderPolicy()

    def keys(self, scope):
        base = "aivideo:resilience:{" + scope + "}"
        return base, base + ":calls", base + ":failures"

    async def acquire(self, scope: str, operation_budget: float) -> Permit:
        token = uuid.uuid4().hex
        raw = await self.client.eval(ACQUIRE, 1, self.keys(scope)[0], token,
                                     operation_budget + 30, self.policy.breaker_recovery_s)
        state, value = (x.decode() if isinstance(x, bytes) else str(x) for x in raw)
        if state in {"open", "auth"}:
            raise CircuitDeferred(float(value) if state == "open" else 60, state)
        return Permit(scope, token, value)

    async def record(self, permit: Permit, outcome: str, retry_after: float = 30) -> None:
        if outcome not in {"success", "failure", "ignore", "rate", "auth"}:
            raise ValueError("invalid circuit outcome")
        p = self.policy
        await self.client.eval(RECORD, 3, *self.keys(permit.scope),
                               permit.generation, permit.token, outcome,
                               p.breaker_window_s, p.breaker_min_calls, p.breaker_threshold,
                               p.breaker_cooldown_s, retry_after)

    async def allow_rate(self, scope: str, limit: int, window_s: int = 60) -> bool:
        return bool(await self.client.eval(RATE, 1, "aivideo:rate:{" + scope + "}",
                                           window_s, limit, uuid.uuid4().hex))

    async def check_credential(self, credential_scope: str) -> None:
        if await self.client.exists("aivideo:credential:paused:" + credential_scope):
            raise CircuitDeferred(60, "credential")

    async def pause_credential(self, credential_scope: str) -> None:
        # No automatic expiry: rotation uses a new versioned reference. Operator
        # unpause must be an explicit audited action, not a retry timer.
        await self.client.set("aivideo:credential:paused:" + credential_scope, "1")
