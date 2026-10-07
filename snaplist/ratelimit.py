"""Per-seller token bucket in Redis.

Each seller has a bucket of `burst` tokens. Every upload takes one token.
Tokens come back at `per_minute / 60` per second. When the bucket is empty the
API answers 429 with a Retry-After header.

The check runs as one Lua script, so it is atomic even with many API servers
sharing the same Redis. The clock is Redis's own (TIME), so API servers whose
clocks differ a little still agree on how many tokens a seller has.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import redis

_TOKEN_BUCKET = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2])
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1]) or capacity
local ts = tonumber(bucket[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - ts) * refill_per_ms)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil(capacity / refill_per_ms) + 1000)
return {allowed, tostring(tokens)}
"""


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after_seconds: int


class TokenBucketLimiter:
    def __init__(self, client: redis.Redis, burst: int, per_minute: int) -> None:
        self.burst = burst
        self.refill_per_ms = per_minute / 60_000.0
        self._script = client.register_script(_TOKEN_BUCKET)

    def check(self, seller_id: str) -> RateDecision:
        allowed, tokens = self._script(
            keys=[f"snaplist:ratelimit:{seller_id}"],
            args=[self.burst, self.refill_per_ms],
        )
        if allowed:
            return RateDecision(True, 0)
        missing = 1.0 - float(tokens)
        return RateDecision(False, max(1, math.ceil(missing / self.refill_per_ms / 1000)))
