"""Shared token buckets in Redis (ADR 0002).

Used for (a) global per-host crawl politeness shared by ALL tenants and jobs and (b) provider /
tenant API rate limits. The bucket update is a single atomic Lua script, so concurrent workers
in different processes cannot exceed the configured rate. Without Redis, an in-process fallback
keeps single-process behaviour correct (documented limitation: not shared across processes).
"""

import asyncio
import threading
import time

from app.core.config import get_settings

_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])      -- tokens per second
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local s = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(s[1]) or burst
local ts = tonumber(s[2]) or now
tokens = math.min(burst, tokens + (now - ts) * rate)
local wait = 0
if tokens >= cost then
  tokens = tokens - cost
else
  wait = (cost - tokens) / rate
end
if wait == 0 then
  redis.call('HSET', key, 'tokens', tokens, 'ts', now)
  redis.call('EXPIRE', key, math.ceil(burst / rate) + 60)
end
return tostring(wait)
"""


class TokenBucket:
    def __init__(self, redis_client=None, prefix: str = "tb"):
        self.prefix = prefix
        if redis_client is None and get_settings().redis_url:
            import redis
            redis_client = redis.Redis.from_url(get_settings().redis_url)
        self.redis = redis_client
        self._script = self.redis.register_script(_LUA) if self.redis is not None else None
        self._local: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    @property
    def shared(self) -> bool:
        return self.redis is not None

    def try_acquire(self, key: str, rate: float, burst: float, cost: float = 1.0) -> float:
        """Take tokens if available. Returns 0 on success, else seconds to wait (nothing taken)."""
        now = time.time()
        if self._script is not None:
            return float(self._script(keys=[f"{self.prefix}:{key}"], args=[rate, burst, now, cost]))
        with self._lock:
            tokens, ts = self._local.get(key, (burst, now))
            tokens = min(burst, tokens + (now - ts) * rate)
            if tokens >= cost:
                self._local[key] = (tokens - cost, now)
                return 0.0
            return (cost - tokens) / rate

    async def acquire(self, key: str, rate: float, burst: float = 1.0, cost: float = 1.0, max_wait: float = 300) -> None:
        waited = 0.0
        while (wait := self.try_acquire(key, rate, burst, cost)) > 0:
            if waited + wait > max_wait:
                raise TimeoutError(f"rate limit wait exceeded for {key}")
            await asyncio.sleep(wait)
            waited += wait


def host_key(host: str) -> str:
    return f"host:{host.lower().rstrip('.')}"
