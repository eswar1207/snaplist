"""HTTP API. Uploads are accepted fast; the heavy work happens in the workers.

Run:  uvicorn snaplist.api:create_app --factory --port 8300
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import redis
from fastapi import FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import imaging, metrics, pipeline
from .config import Settings
from .jobs import JobStore, now_ms
from .ratelimit import TokenBucketLimiter
from .storage import LocalStorage, original_key, output_key

SELLER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
FILE_NAME = re.compile(r"^[a-z0-9-]+\.(jpg|png|mp4)$")
EXTENSIONS = {"JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png", "WEBP": ".webp"}
MEDIA_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".mp4": "video/mp4", ".webp": "image/webp"}
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    client = redis.Redis.from_url(settings.redis_url)
    store = JobStore(client, list(settings.tier_models))
    storage = LocalStorage(settings.storage_dir)
    limiter = TokenBucketLimiter(client, settings.rate_limit_burst, settings.rate_limit_per_minute)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        store.ensure_queues()
        yield
        client.close()

    app = FastAPI(title="SnapList", version="1.0.0", lifespan=lifespan,
                  description="Turns a seller's phone photo into Amazon-ready listing images.")

    def reject(status: int, reason: str, detail: str, headers: dict | None = None) -> HTTPException:
        metrics.JOBS_REJECTED.labels(reason).inc()
        metrics.API_REQUESTS.labels("submit", str(status)).inc()
        return HTTPException(status_code=status, detail=detail, headers=headers)

    def existing_job_response(job_id: str, reason: str) -> JSONResponse:
        metrics.JOBS_REUSED.labels(reason).inc()
        metrics.API_REQUESTS.labels("submit", "200").inc()
        job = store.get(job_id)
        body = describe(job) if job else {"job_id": job_id, "state": "queued"}
        return JSONResponse(body, status_code=200, headers={"X-SnapList-Reused": reason})

    # All endpoints are plain `def`: FastAPI runs them in a thread pool, and the
    # blocking Redis client and file writes stay simple and correct.
    @app.post("/v1/jobs", status_code=202)
    def submit_job(
        response: Response,
        image: UploadFile = File(..., description="Product photo (JPEG, PNG or WEBP)"),
        tier: str = Form("standard", description="standard (fast) or high (cleaner edges, slower)"),
        outputs: str = Form("studio", description="extra outputs: any of studio, cutout, video"),
        fault: str | None = Form(None, description="test-only fault injection"),
        x_seller_id: str = Header(..., description="Seller account id"),
        idempotency_key: str | None = Header(None),
    ):
        # 1. Validate the cheap things first.
        if not SELLER_ID.match(x_seller_id):
            raise reject(400, "bad_request", "X-Seller-Id must be 1-64 letters, digits, '-' or '_'")
        if tier not in settings.tier_models:
            raise reject(400, "bad_request", f"tier must be one of {sorted(settings.tier_models)}")
        try:
            extras = pipeline.parse_outputs(outputs)
        except ValueError as exc:
            raise reject(400, "bad_request", str(exc)) from exc
        if fault and not settings.allow_fault_injection:
            raise reject(400, "bad_request", "fault injection is disabled")
        if idempotency_key is not None and not IDEMPOTENCY_KEY.match(idempotency_key):
            raise reject(400, "bad_request", "Idempotency-Key must be 8-128 letters, digits, '-' or '_'")

        # 2. Per-seller rate limit, before we spend time reading the upload.
        decision = limiter.check(x_seller_id)
        if not decision.allowed:
            raise reject(429, "rate_limited", "too many uploads; slow down",
                         {"Retry-After": str(decision.retry_after_seconds)})

        # 3. Read the upload with a size cap and check the image header.
        data = image.file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise reject(413, "too_large", f"upload is larger than {settings.max_upload_bytes} bytes")
        try:
            info = imaging.inspect_image(data)
        except imaging.InvalidImageError as exc:
            raise reject(400, "invalid_image", str(exc)) from exc

        # 4. Same photo + same options = same result. Fingerprint the request.
        content_sha256 = hashlib.sha256(data).hexdigest()
        fingerprint = hashlib.sha256(f"{content_sha256}|{tier}|{','.join(extras)}|{fault or ''}".encode()).hexdigest()
        idem_key = f"snaplist:idem:{x_seller_id}:{idempotency_key}" if idempotency_key else None
        dedupe_key = f"snaplist:dedupe:{x_seller_id}:{fingerprint}"

        if idem_key:
            previous = store.r.get(idem_key)
            if previous:
                previous_job, _, previous_fp = previous.decode().partition("|")
                if previous_fp != fingerprint:
                    raise reject(422, "idempotency_conflict", "Idempotency-Key was already used with a different request")
                return existing_job_response(previous_job, "idempotency_key")

        duplicate = store.r.get(dedupe_key)
        if duplicate:
            duplicate_job = store.get(duplicate.decode())
            if duplicate_job and duplicate_job.get("state") != "failed":
                if idem_key:
                    store.r.set(idem_key, f"{duplicate_job['id']}|{fingerprint}", ex=settings.dedupe_ttl_seconds)
                return existing_job_response(duplicate_job["id"], "duplicate_upload")

        # 5. Load shedding: refuse new work early instead of letting the queue grow without limit.
        if store.open_jobs(tier) >= settings.max_open_jobs_per_tier:
            raise reject(503, "overloaded", f"the {tier} queue is full; try again soon", {"Retry-After": "5"})

        # 6. Claim the dedupe and idempotency keys (SET NX) so concurrent identical
        #    requests create only one job.
        job_id = uuid.uuid4().hex
        dedupe_claim = store.claim(dedupe_key, job_id, settings.dedupe_ttl_seconds)
        if not dedupe_claim.claimed:
            winner = store.get(dedupe_claim.existing or "")
            if winner and winner.get("state") != "failed":
                return existing_job_response(winner["id"], "duplicate_upload")
            store.r.set(dedupe_key, job_id, ex=settings.dedupe_ttl_seconds)  # previous attempt failed: retry it
        if idem_key:
            idem_claim = store.claim(idem_key, f"{job_id}|{fingerprint}", settings.dedupe_ttl_seconds)
            if not idem_claim.claimed:
                store.release(dedupe_key)
                previous_job, _, previous_fp = (idem_claim.existing or "").partition("|")
                if previous_fp != fingerprint:
                    raise reject(422, "idempotency_conflict", "Idempotency-Key was already used with a different request")
                return existing_job_response(previous_job, "idempotency_key")

        # 7. Store the original photo, then create the job and enqueue it atomically.
        key = original_key(job_id, EXTENSIONS[info.format])
        job = {
            "id": job_id, "seller_id": x_seller_id, "tier": tier, "model": settings.tier_models[tier],
            "state": "queued", "attempts": "0", "created_at": str(now_ms()), "original_key": key,
            "content_sha256": content_sha256, "extras": ",".join(extras), "fault": fault or "",
            "width": str(info.width), "height": str(info.height), "format": info.format,
        }
        try:
            storage.put(key, data)
            store.create_and_enqueue(job, settings.job_ttl_seconds)
        except Exception:
            store.release(*(k for k in (dedupe_key, idem_key) if k))
            storage.delete(key)
            raise

        metrics.JOBS_SUBMITTED.labels(tier).inc()
        metrics.API_REQUESTS.labels("submit", "202").inc()
        response.headers["Location"] = f"/v1/jobs/{job_id}"
        return describe(job)

    def describe(job: dict[str, str]) -> dict:
        job_id = job["id"]
        created = int(job["created_at"])
        first_started = int(job["first_started_at"]) if job.get("first_started_at") else None
        finished = int(job["finished_at"]) if job.get("finished_at") else None
        files = json.loads(job.get("outputs") or "[]")
        return {
            "job_id": job_id,
            "state": job["state"],
            "tier": job["tier"],
            "model": job["model"],
            "attempts": int(job.get("attempts", 0)),
            "created_at_ms": created,
            "queue_wait_ms": first_started - created if first_started else None,
            "total_ms": finished - created if finished else None,
            "timings_ms": json.loads(job["timings"]) if job.get("timings") else None,
            "original_url": f"/v1/jobs/{job_id}/original",
            "outputs": {name: f"/v1/jobs/{job_id}/files/{name}" for name in files},
            "compliance": json.loads(job["report"]) if job.get("report") else None,
            "error": job.get("error") or None,
            "last_error": job.get("last_error") or None,
            "status_url": f"/v1/jobs/{job_id}",
        }

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str):
        job = store.get(job_id) if re.fullmatch(r"[0-9a-f]{32}", job_id) else None
        if job is None:
            metrics.API_REQUESTS.labels("status", "404").inc()
            raise HTTPException(404, "job not found")
        metrics.API_REQUESTS.labels("status", "200").inc()
        return describe(job)

    @app.get("/v1/jobs/{job_id}/files/{name}")
    def get_file(job_id: str, name: str):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id) or not FILE_NAME.match(name):
            raise HTTPException(404, "file not found")
        key = output_key(job_id, name)
        if not storage.exists(key):
            raise HTTPException(404, "file not found")
        return FileResponse(storage.path(key), media_type=MEDIA_TYPES[Path(name).suffix])

    @app.get("/v1/jobs/{job_id}/original")
    def get_original(job_id: str):
        job = store.get(job_id) if re.fullmatch(r"[0-9a-f]{32}", job_id) else None
        if job is None or not storage.exists(job["original_key"]):
            raise HTTPException(404, "file not found")
        return FileResponse(storage.path(job["original_key"]), media_type=MEDIA_TYPES[Path(job["original_key"]).suffix])

    @app.get("/v1/stats")
    def stats():
        return store.stats()

    @app.get("/health")
    def health():
        try:
            client.ping()
        except redis.RedisError:
            return JSONResponse({"status": "unavailable", "redis": False}, status_code=503)
        return {"status": "ok", "redis": True}

    @app.get("/metrics")
    def prometheus_metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC_DIR / "index.html").read_text()

    return app
