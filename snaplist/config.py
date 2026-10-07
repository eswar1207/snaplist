"""All settings in one place, read from environment variables (12-factor style)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Each quality tier has its own queue and its own workers, so slow high-quality
# jobs never block fast standard jobs (no head-of-line blocking).
TIER_MODELS: dict[str, str] = {
    "standard": "u2netp",  # ~0.5 s per image on one CPU core
    "high": "isnet-general-use",  # ~3 s per image on one CPU core, cleaner edges
}


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    redis_url: str = "redis://127.0.0.1:6391/0"
    storage_dir: Path = PROJECT_ROOT / "data"
    model_dir: Path = PROJECT_ROOT / "models"
    max_upload_bytes: int = 15 * 1024 * 1024
    max_open_jobs_per_tier: int = 500  # load shedding: queued + processing jobs allowed per tier
    rate_limit_burst: int = 20  # token bucket size per seller
    rate_limit_per_minute: int = 60  # refill speed per seller
    visibility_timeout_ms: int = 30_000  # an unacknowledged job is retried after this long
    max_attempts: int = 3
    batch_size: int = 1  # images per model call; 1 is fastest on CPU (see BENCHMARKS.md)
    batch_wait_ms: int = 20  # how long a worker waits to fill a batch when batch_size > 1
    read_block_ms: int = 1000  # how long a worker blocks waiting for new jobs
    job_ttl_seconds: int = 7 * 24 * 3600
    dedupe_ttl_seconds: int = 24 * 3600
    max_input_side_px: int = 2048  # bigger photos are shrunk first; the output is only 2000 px
    allow_fault_injection: bool = False  # tests and benchmarks only
    tier_models: dict[str, str] = field(default_factory=lambda: dict(TIER_MODELS))

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            redis_url=os.environ.get("SNAPLIST_REDIS_URL", cls.redis_url),
            storage_dir=Path(os.environ.get("SNAPLIST_STORAGE_DIR", cls.storage_dir)),
            model_dir=Path(os.environ.get("SNAPLIST_MODEL_DIR", cls.model_dir)),
            max_upload_bytes=_env_int("SNAPLIST_MAX_UPLOAD_BYTES", cls.max_upload_bytes),
            max_open_jobs_per_tier=_env_int("SNAPLIST_MAX_OPEN_JOBS", cls.max_open_jobs_per_tier),
            rate_limit_burst=_env_int("SNAPLIST_RATE_LIMIT_BURST", cls.rate_limit_burst),
            rate_limit_per_minute=_env_int("SNAPLIST_RATE_LIMIT_PER_MINUTE", cls.rate_limit_per_minute),
            visibility_timeout_ms=_env_int("SNAPLIST_VISIBILITY_TIMEOUT_MS", cls.visibility_timeout_ms),
            max_attempts=_env_int("SNAPLIST_MAX_ATTEMPTS", cls.max_attempts),
            batch_size=_env_int("SNAPLIST_BATCH_SIZE", cls.batch_size),
            batch_wait_ms=_env_int("SNAPLIST_BATCH_WAIT_MS", cls.batch_wait_ms),
            read_block_ms=_env_int("SNAPLIST_READ_BLOCK_MS", cls.read_block_ms),
            job_ttl_seconds=_env_int("SNAPLIST_JOB_TTL_SECONDS", cls.job_ttl_seconds),
            dedupe_ttl_seconds=_env_int("SNAPLIST_DEDUPE_TTL_SECONDS", cls.dedupe_ttl_seconds),
            max_input_side_px=_env_int("SNAPLIST_MAX_INPUT_SIDE_PX", cls.max_input_side_px),
            allow_fault_injection=_env_bool("SNAPLIST_ALLOW_FAULT_INJECTION"),
        )
