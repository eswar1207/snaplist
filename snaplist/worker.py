"""Worker process: takes jobs from one tier's queue and turns photos into listing images.

Delivery is at-least-once. A job is acknowledged (XACK) only after its outputs
are stored and its record says "succeeded" or "failed". If a worker dies in the
middle, the job stays in the stream's pending list; after `visibility_timeout_ms`
another worker takes it over with XAUTOCLAIM. Writes are idempotent (same keys,
atomic replace) and the final state is set at most once, so a second delivery
cannot create a duplicate result.

Run:  python -m snaplist.worker --tier standard --worker-id w1 --metrics-port 9101
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import time
from dataclasses import dataclass, field

import redis
from prometheus_client import start_http_server

from . import imaging, metrics, pipeline
from .config import Settings
from .jobs import GROUP, TERMINAL_STATES, JobStore, now_ms, stream_key
from .model import MODEL_SPECS, Segmenter
from .storage import LocalStorage, output_key

log = logging.getLogger("snaplist.worker")


class InjectedTransientError(RuntimeError):
    """Raised only by fault injection in tests and benchmarks."""


@dataclass
class Task:
    message_id: bytes
    job_id: str
    job: dict[str, str]
    attempts: int
    timings_ms: dict[str, float] = field(default_factory=dict)


class Worker:
    def __init__(self, settings: Settings, tier: str, worker_id: str, client: redis.Redis,
                 segmenter: Segmenter | None = None) -> None:
        self.s = settings
        self.tier = tier
        self.worker_id = worker_id
        self.r = client
        self.stream = stream_key(tier)
        self.store = JobStore(client, list(settings.tier_models))
        self.storage = LocalStorage(settings.storage_dir)
        model_name = settings.tier_models[tier]
        self.segmenter = segmenter or Segmenter(MODEL_SPECS[model_name], settings.model_dir)
        self.model_name = model_name
        self._stop = False
        self._next_reclaim = 0.0
        self._next_heartbeat = 0.0
        self.jobs_done = 0
        self.started_at = now_ms()

    # ---- main loop -----------------------------------------------------
    def run(self) -> None:
        self.store.ensure_queues()
        log.info("worker %s started on tier=%s model=%s", self.worker_id, self.tier, self.model_name)
        while not self._stop:
            self._heartbeat()
            messages = self._reclaim_stale() or self._read_new()
            if messages:
                self._process(messages)
        log.info("worker %s stopped after %d jobs", self.worker_id, self.jobs_done)

    def stop(self, *_args) -> None:
        self._stop = True  # finish the current batch, then exit

    # ---- getting work --------------------------------------------------
    def _read_new(self) -> list:
        response = self.r.xreadgroup(GROUP, self.worker_id, {self.stream: ">"},
                                     count=self.s.batch_size, block=self.s.read_block_ms)
        messages = list(response[0][1]) if response else []
        # Dynamic batching: if we got fewer than batch_size, wait a short time for more.
        if messages and len(messages) < self.s.batch_size and self.s.batch_wait_ms > 0:
            deadline = time.monotonic() + self.s.batch_wait_ms / 1000
            while len(messages) < self.s.batch_size:
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    break
                more = self.r.xreadgroup(GROUP, self.worker_id, {self.stream: ">"},
                                         count=self.s.batch_size - len(messages), block=remaining_ms)
                if not more:
                    break
                messages.extend(more[0][1])
        return messages

    def _reclaim_stale(self) -> list:
        """Take over jobs that another worker received but never acknowledged."""
        if time.monotonic() < self._next_reclaim:
            return []
        self._next_reclaim = time.monotonic() + 1.0
        # Reply: [next start id, claimed messages, (Redis 7+) ids already deleted from the stream]
        reply = self.r.xautoclaim(
            self.stream, GROUP, self.worker_id,
            min_idle_time=self.s.visibility_timeout_ms, start_id="0-0", count=self.s.batch_size,
        )
        claimed = [(message_id, fields) for message_id, fields in reply[1] if fields]
        if claimed:
            metrics.JOBS_RECLAIMED.labels(self.tier).inc(len(claimed))
            log.warning("worker %s took over %d stale job(s)", self.worker_id, len(claimed))
        return claimed

    # ---- processing ----------------------------------------------------
    def _process(self, messages: list) -> None:
        tasks: list[Task] = []
        for message_id, fields in messages:
            job_id = fields[b"job_id"].decode()
            job = self.store.get(job_id)
            if job is None or job.get("state") in TERMINAL_STATES:
                self.r.xack(self.stream, GROUP, message_id)  # expired job or a repeated delivery
                continue
            attempts = self.store.start_attempt(job_id, self.worker_id)
            tasks.append(Task(message_id, job_id, job, attempts))
        if not tasks:
            return
        metrics.BATCH_SIZE.labels(self.tier).observe(len(tasks))

        # 1. Decode each photo. A bad photo fails alone and does not affect the batch.
        ready: list[tuple[Task, object]] = []
        for task in tasks:
            try:
                self._inject_faults(task)
                started = time.perf_counter()
                original = self.storage.get(task.job["original_key"])
                image = pipeline.prepare(original, self.s.max_input_side_px)
                self._time(task, "decode", started)
                ready.append((task, image))
            except imaging.InvalidImageError as exc:
                self._fail_permanently(task, str(exc))
            except Exception as exc:  # noqa: BLE001 - any other error may be temporary
                self._fail_or_retry(task, exc)
        if not ready:
            return

        # 2. One model call for the whole batch.
        try:
            started = time.perf_counter()
            masks = self.segmenter.predict([image for _, image in ready])
            model_seconds = time.perf_counter() - started
        except Exception as exc:  # noqa: BLE001
            for task, _ in ready:
                self._fail_or_retry(task, exc)
            return

        # 3. Render, store and finish each job.
        for (task, image), mask in zip(ready, masks):
            task.timings_ms["model_ms"] = round(model_seconds * 1000 / len(ready), 1)
            metrics.STAGE_SECONDS.labels(self.tier, "model").observe(model_seconds / len(ready))
            try:
                started = time.perf_counter()
                mask = imaging.clean_mask(mask)
                extras = pipeline.parse_outputs(task.job.get("extras", ""))
                result = pipeline.render(image, mask, extras)
                self._time(task, "render", started)
                started = time.perf_counter()
                for name, data in result.files.items():
                    self.storage.put(output_key(task.job_id, name), data)
                self._time(task, "store", started)
                self._succeed(task, result)
            except imaging.InvalidImageError as exc:
                self._fail_permanently(task, str(exc))
            except Exception as exc:  # noqa: BLE001
                self._fail_or_retry(task, exc)

    def _time(self, task: Task, stage: str, started: float) -> None:
        seconds = time.perf_counter() - started
        task.timings_ms[f"{stage}_ms"] = round(seconds * 1000, 1)
        metrics.STAGE_SECONDS.labels(self.tier, stage).observe(seconds)

    def _succeed(self, task: Task, result: pipeline.RenderResult) -> None:
        fields = {
            "outputs": json.dumps(sorted(result.files)),
            "report": json.dumps(result.report),
            "timings": json.dumps(task.timings_ms | {k: round(v, 1) for k, v in result.timings_ms.items()}),
            "error": "",
        }
        if self.store.finish(task.job_id, self.tier, "succeeded", fields):
            metrics.JOBS_FINISHED.labels(self.tier, "succeeded").inc()
            metrics.JOB_SECONDS.labels(self.tier).observe((now_ms() - int(task.job["created_at"])) / 1000)
            self.jobs_done += 1
        self.r.xack(self.stream, GROUP, task.message_id)

    def _fail_permanently(self, task: Task, reason: str) -> None:
        if self.store.finish(task.job_id, self.tier, "failed", {"error": reason[:500]}):
            metrics.JOBS_FINISHED.labels(self.tier, "failed").inc()
            self.store.send_to_dlq(task.job_id, self.tier, reason)
        self.r.xack(self.stream, GROUP, task.message_id)
        log.warning("job %s failed: %s", task.job_id, reason)

    def _fail_or_retry(self, task: Task, exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        if task.attempts >= self.s.max_attempts:
            self._fail_permanently(task, f"gave up after {task.attempts} attempts; last error: {error}")
            return
        # Do not acknowledge: the job stays pending and is retried after the
        # visibility timeout (that delay is the retry back-off).
        self.store.record_retry(task.job_id, error)
        metrics.JOB_RETRIES.labels(self.tier).inc()
        log.warning("job %s attempt %d failed, will retry: %s", task.job_id, task.attempts, error)

    def _inject_faults(self, task: Task) -> None:
        """Test-only fault injection, enabled by SNAPLIST_ALLOW_FAULT_INJECTION=1.

        fault = "transient:N"  fail the first N attempts
                "slow:MS"      sleep MS milliseconds (gives a test time to kill the worker)
                "crash"        kill this worker process on the first attempt
        """
        fault = task.job.get("fault", "")
        if not fault or not self.s.allow_fault_injection:
            return
        kind, _, value = fault.partition(":")
        if kind == "transient" and task.attempts <= int(value or 1):
            raise InjectedTransientError(f"injected failure on attempt {task.attempts}")
        if kind == "slow":
            time.sleep(int(value or 1000) / 1000)
        if kind == "crash" and task.attempts == 1:
            os._exit(17)  # simulate a hard crash (no cleanup, no acknowledgement)

    # ---- liveness ------------------------------------------------------
    def _heartbeat(self) -> None:
        if time.monotonic() < self._next_heartbeat:
            return
        self._next_heartbeat = time.monotonic() + 2.0
        self.store.heartbeat(self.worker_id, {
            "worker_id": self.worker_id, "tier": self.tier, "model": self.model_name,
            "pid": os.getpid(), "jobs_done": self.jobs_done, "started_at": self.started_at,
        })


def main() -> None:
    parser = argparse.ArgumentParser(description="SnapList worker")
    parser.add_argument("--tier", default="standard")
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-{os.getpid()}")
    parser.add_argument("--metrics-port", type=int, default=0, help="0 = do not serve metrics")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = Settings.from_env()
    if args.tier not in settings.tier_models:
        parser.error(f"unknown tier {args.tier}; choose from {list(settings.tier_models)}")
    if args.metrics_port:
        start_http_server(args.metrics_port)
    worker = Worker(settings, args.tier, args.worker_id, redis.Redis.from_url(settings.redis_url))
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run()


if __name__ == "__main__":
    main()
