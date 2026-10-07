"""Job records and queues in Redis.

Data model (all keys start with "snaplist:"):

  job:<id>              HASH   the job record (state, timings, outputs, report, error)
  jobs:<tier>           STREAM the queue for one quality tier; consumer group "workers"
  open:<tier>           STRING number of queued + processing jobs (used for load shedding)
  dlq                   STREAM jobs that failed for good (dead-letter queue)
  dedupe:<seller>:<fp>  STRING same seller + same photo + same options -> existing job id
  idem:<seller>:<key>   STRING Idempotency-Key -> "<job id>|<fingerprint>"
  worker:<id>           STRING worker heartbeat (expires if the worker dies)

Why Redis Streams for the queue: a consumer group gives each job to one worker,
keeps it in a "pending" list until the worker acknowledges it (XACK), and lets
another worker take over a job whose worker died (XAUTOCLAIM). That is
at-least-once delivery; the job state check below makes repeated delivery harmless.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import redis

PREFIX = "snaplist:"
GROUP = "workers"
DLQ_STREAM = f"{PREFIX}dlq"
TERMINAL_STATES = {"succeeded", "failed"}
STREAM_MAX_LEN = 100_000  # approximate trim so the stream cannot grow forever

# Moves a job to a final state at most once and decrements the open-jobs counter
# in the same atomic step. Two workers finishing the same job (after a takeover)
# therefore cannot double-count it.
_FINISH_SCRIPT = """
local state = redis.call('HGET', KEYS[1], 'state')
if not state or state == 'succeeded' or state == 'failed' then
  return 0
end
redis.call('HSET', KEYS[1], unpack(ARGV))
redis.call('DECR', KEYS[2])
return 1
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def job_key(job_id: str) -> str:
    return f"{PREFIX}job:{job_id}"


def stream_key(tier: str) -> str:
    return f"{PREFIX}jobs:{tier}"


def open_key(tier: str) -> str:
    return f"{PREFIX}open:{tier}"


@dataclass(frozen=True)
class Claim:
    claimed: bool
    existing: str | None


class JobStore:
    def __init__(self, client: redis.Redis, tiers: list[str]) -> None:
        self.r = client
        self.tiers = tiers
        self._finish = self.r.register_script(_FINISH_SCRIPT)

    # ---- setup ---------------------------------------------------------
    def ensure_queues(self) -> None:
        for tier in self.tiers:
            try:
                self.r.xgroup_create(stream_key(tier), GROUP, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):  # group already exists: fine
                    raise

    # ---- claims for dedupe and idempotency -----------------------------
    def claim(self, key: str, value: str, ttl_seconds: int) -> Claim:
        """SET NX: the first request wins; later ones learn the existing value."""
        if self.r.set(key, value, nx=True, ex=ttl_seconds):
            return Claim(True, None)
        existing = self.r.get(key)
        return Claim(False, existing.decode() if existing else None)

    def release(self, *keys: str) -> None:
        if keys:
            self.r.delete(*keys)

    # ---- job lifecycle -------------------------------------------------
    def open_jobs(self, tier: str) -> int:
        value = self.r.get(open_key(tier))
        return int(value) if value else 0

    def create_and_enqueue(self, job: dict[str, str], ttl_seconds: int) -> None:
        """Store the job record and put it on its tier's queue in one transaction."""
        key = job_key(job["id"])
        pipe = self.r.pipeline(transaction=True)
        pipe.hset(key, mapping=job)
        pipe.expire(key, ttl_seconds)
        pipe.incr(open_key(job["tier"]))
        pipe.xadd(stream_key(job["tier"]), {"job_id": job["id"]}, maxlen=STREAM_MAX_LEN, approximate=True)
        pipe.execute()

    def get(self, job_id: str) -> dict[str, str] | None:
        raw = self.r.hgetall(job_key(job_id))
        if not raw:
            return None
        return {k.decode(): v.decode() for k, v in raw.items()}

    def start_attempt(self, job_id: str, worker_id: str) -> int:
        key = job_key(job_id)
        started = now_ms()
        pipe = self.r.pipeline(transaction=True)
        pipe.hincrby(key, "attempts", 1)
        pipe.hset(key, mapping={"state": "processing", "worker_id": worker_id, "started_at": started})
        pipe.hsetnx(key, "first_started_at", started)  # kept from the first attempt: measures queue wait
        attempts, _, _ = pipe.execute()
        return int(attempts)

    def record_retry(self, job_id: str, error: str) -> None:
        self.r.hset(job_key(job_id), mapping={"state": "retrying", "last_error": error[:500]})

    def finish(self, job_id: str, tier: str, state: str, fields: dict[str, str]) -> bool:
        """Set a final state. Returns False if the job was already final."""
        assert state in TERMINAL_STATES
        args: list[str] = ["state", state, "finished_at", str(now_ms())]
        for name, value in fields.items():
            args.extend([name, value])
        return bool(self._finish(keys=[job_key(job_id), open_key(tier)], args=args))

    def send_to_dlq(self, job_id: str, tier: str, reason: str) -> None:
        self.r.xadd(DLQ_STREAM, {"job_id": job_id, "tier": tier, "reason": reason[:500]},
                    maxlen=STREAM_MAX_LEN, approximate=True)

    # ---- workers -------------------------------------------------------
    def heartbeat(self, worker_id: str, info: dict, ttl_seconds: int = 15) -> None:
        self.r.set(f"{PREFIX}worker:{worker_id}", json.dumps(info), ex=ttl_seconds)

    def stats(self) -> dict:
        tiers = {}
        for tier in self.tiers:
            pending = self.r.xpending(stream_key(tier), GROUP)
            tiers[tier] = {
                "open_jobs": self.open_jobs(tier),
                "pending_unacknowledged": int(pending["pending"]) if pending else 0,
                "stream_length": self.r.xlen(stream_key(tier)),
            }
        workers = []
        for key in self.r.scan_iter(f"{PREFIX}worker:*"):
            value = self.r.get(key)
            if value:
                workers.append(json.loads(value))
        return {"tiers": tiers, "workers": workers, "dead_letter_queue": self.r.xlen(DLQ_STREAM)}
