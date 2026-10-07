"""Prometheus metrics. The API serves them on /metrics; each worker on its own port."""

from prometheus_client import Counter, Histogram

API_REQUESTS = Counter("snaplist_api_requests_total", "API requests by endpoint and status", ["endpoint", "status"])
JOBS_SUBMITTED = Counter("snaplist_jobs_submitted_total", "New jobs accepted", ["tier"])
JOBS_REUSED = Counter("snaplist_jobs_reused_total", "Requests answered with an existing job", ["reason"])
JOBS_REJECTED = Counter("snaplist_jobs_rejected_total", "Uploads rejected", ["reason"])

JOBS_FINISHED = Counter("snaplist_jobs_finished_total", "Jobs that reached a final state", ["tier", "outcome"])
JOBS_RECLAIMED = Counter("snaplist_jobs_reclaimed_total", "Jobs taken over from a stuck or dead worker", ["tier"])
JOB_RETRIES = Counter("snaplist_job_retries_total", "Attempts that failed and will be retried", ["tier"])
STAGE_SECONDS = Histogram(
    "snaplist_stage_seconds", "Time per processing stage", ["tier", "stage"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16),
)
JOB_SECONDS = Histogram(
    "snaplist_job_seconds", "Upload-to-finished time per job", ["tier"],
    buckets=(0.5, 1, 2, 4, 8, 16, 32, 64, 128, 256),
)
BATCH_SIZE = Histogram("snaplist_worker_batch_size", "Jobs per model call", ["tier"], buckets=(1, 2, 4, 8, 16))
