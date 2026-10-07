"""SnapList benchmarks. Every number in BENCHMARKS.md comes from this script.

It starts a real stack on this machine (redis-server, the API under uvicorn,
worker processes), sends synthetic product photos, and reads the timings that
the workers store in each job record.

Usage:
    python bench/run_benchmarks.py all          # everything (about 15 minutes on 2 vCPUs)
    python bench/run_benchmarks.py throughput   # or several of: model render throughput steady chaos overload api

`steady` reads benchmarks/results/throughput.json, so run `throughput` first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
import numpy as np
import redis

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from snaplist.model import MODEL_SPECS, Segmenter  # noqa: E402
from tests.photos import photo_bytes, product_photo  # noqa: E402

RESULTS = ROOT / "benchmarks" / "results"
PY = sys.executable


def percentile(values: list[float], p: float) -> float:
    return round(float(np.percentile(values, p)), 1) if values else float("nan")


def summary(values: list[float]) -> dict:
    return {"p50": percentile(values, 50), "p95": percentile(values, 95), "p99": percentile(values, 99),
            "max": round(max(values), 1) if values else None, "count": len(values)}


def environment() -> dict:
    load1, load5, load15 = os.getloadavg()
    mem_kb = int(next(line for line in open("/proc/meminfo") if line.startswith("MemTotal")).split()[1])
    return {
        "cpus": os.cpu_count(), "ram_gb": round(mem_kb / 1024 / 1024, 1),
        "python": platform.python_version(), "os": platform.platform(),
        "load_average_at_start": [round(load1, 2), round(load5, 2), round(load15, 2)],
    }


def save(name: str, data: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    data = {"scenario": name, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "environment": environment()} | data
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
    print(json.dumps(data, indent=2))


# --------------------------------------------------------------------------- stack
class Stack:
    """redis-server + API + worker processes, each a real OS process."""

    def __init__(self, redis_port: int = 6392, api_port: int = 8301, **settings: str) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="snaplist-bench-"))
        self.redis_port, self.api_port = redis_port, api_port
        self.env = os.environ | {
            "SNAPLIST_REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
            "SNAPLIST_STORAGE_DIR": str(self.workdir / "data"),
            "SNAPLIST_RATE_LIMIT_BURST": "100000",
            "SNAPLIST_RATE_LIMIT_PER_MINUTE": "1000000",
            "OMP_NUM_THREADS": "1",
        } | {f"SNAPLIST_{k.upper()}": str(v) for k, v in settings.items()}
        self.redis_process: subprocess.Popen | None = None
        self.api_process: subprocess.Popen | None = None
        self.workers: list[subprocess.Popen] = []
        self.url = f"http://127.0.0.1:{api_port}"

    def __enter__(self) -> "Stack":
        self.redis_process = subprocess.Popen(
            ["redis-server", "--port", str(self.redis_port), "--save", "", "--appendonly", "no",
             "--dir", str(self.workdir)], stdout=subprocess.DEVNULL)
        self.redis = redis.Redis(port=self.redis_port)
        self._wait(lambda: self.redis.ping())
        self.api_process = subprocess.Popen(
            [PY, "-m", "uvicorn", "snaplist.api:create_app", "--factory", "--port", str(self.api_port),
             "--log-level", "warning"], cwd=ROOT, env=self.env)
        self._wait(lambda: self._get("/health"))
        return self

    def __exit__(self, *_exc) -> None:
        for process in [*self.workers, self.api_process, self.redis_process]:
            if process and process.poll() is None:
                process.terminate()
        for process in [*self.workers, self.api_process, self.redis_process]:
            if process:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def start_workers(self, count: int, tier: str = "standard") -> None:
        for _ in range(count):
            worker_id = f"{tier}-{len(self.workers) + 1}"
            self.workers.append(subprocess.Popen(
                [PY, "-m", "snaplist.worker", "--tier", tier, "--worker-id", worker_id],
                cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        alive = {w.pid for w in self.workers if w.poll() is None}
        self._wait(lambda: len({w["pid"] for w in self.stats()["workers"]} & alive) >= len(alive), timeout=60)

    def kill_worker(self, index: int) -> None:
        self.workers[index].send_signal(signal.SIGKILL)  # no cleanup, like a machine dying
        self.workers[index].wait()

    def stats(self) -> dict:
        return self._get("/v1/stats")

    def _get(self, path: str):
        import urllib.request
        with urllib.request.urlopen(self.url + path, timeout=5) as response:
            return json.loads(response.read())

    @staticmethod
    def _wait(check, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if check():
                    return
            except Exception:  # noqa: BLE001 - not ready yet
                pass
            time.sleep(0.1)
        raise TimeoutError("stack did not become ready")


# --------------------------------------------------------------------------- client
async def submit_all(url: str, photos: list[bytes], concurrency: int, outputs: str = "",
                     seller: str = "bench", tier: str = "standard") -> list[dict]:
    """Upload every photo; return status code, latency and job id for each upload."""
    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict] = []

    async def one(session: aiohttp.ClientSession, index: int, photo: bytes) -> None:
        form = aiohttp.FormData()
        form.add_field("image", photo, filename=f"p{index}.jpg", content_type="image/jpeg")
        form.add_field("tier", tier)
        form.add_field("outputs", outputs)
        async with semaphore:
            started = time.perf_counter()
            async with session.post(f"{url}/v1/jobs", data=form, headers={"X-Seller-Id": seller}) as response:
                body = await response.json()
                results.append({"status": response.status, "ms": (time.perf_counter() - started) * 1000,
                                "job_id": body.get("job_id")})

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(one(session, i, p) for i, p in enumerate(photos)))
    return results


def wait_until_idle(stack: Stack, tier: str = "standard", timeout: float = 1800) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stack.stats()["tiers"][tier]["open_jobs"] == 0:
            return
        time.sleep(0.5)
    raise TimeoutError("jobs did not finish")


def job_records(stack: Stack, job_ids: list[str]) -> list[dict]:
    return [stack._get(f"/v1/jobs/{job_id}") for job_id in job_ids]


def analyse(jobs: list[dict]) -> dict:
    done = [j for j in jobs if j["state"] == "succeeded"]
    created = [j["created_at_ms"] for j in jobs]
    finished = [j["created_at_ms"] + j["total_ms"] for j in done]
    span_s = (max(finished) - min(created)) / 1000 if done else float("nan")
    stages: dict[str, list[float]] = {}
    for job in done:
        for stage, ms in (job["timings_ms"] or {}).items():
            stages.setdefault(stage, []).append(ms)
    return {
        "jobs": len(jobs), "succeeded": len(done), "failed": sum(j["state"] == "failed" for j in jobs),
        "images_per_minute": round(len(done) / span_s * 60, 1),
        "end_to_end_ms": summary([j["total_ms"] for j in done]),
        "queue_wait_ms": summary([j["queue_wait_ms"] for j in done]),
        "stage_median_ms": {stage: percentile(values, 50) for stage, values in sorted(stages.items())},
        "compliance_passed": sum(1 for j in done if j["compliance"] and j["compliance"]["passed"]),
        "jobs_with_retries": sum(1 for j in done if j["attempts"] > 1),
    }


def photos(count: int, offset: int = 0, size: tuple[int, int] = (1600, 1200)) -> list[bytes]:
    return [photo_bytes(seed=offset + i, width=size[0], height=size[1]) for i in range(count)]


# --------------------------------------------------------------------------- scenarios
def scenario_model() -> None:
    """The model alone on one CPU thread: cost per image, and whether batching helps."""
    images = [product_photo(1600, 1200, seed)[0] for seed in range(8)]
    rows = []
    for name, batch_sizes in (("u2netp", (1, 2, 4, 8)), ("isnet-general-use", (1, 2))):
        segmenter = Segmenter(MODEL_SPECS[name], ROOT / "models", threads=1)
        segmenter.predict(images[:1])  # warm-up
        for batch in batch_sizes:
            timings = []
            for _ in range(5 if name == "u2netp" else 3):
                started = time.perf_counter()
                segmenter.predict(images[:batch])
                timings.append((time.perf_counter() - started) * 1000)
            median = statistics.median(timings)
            rows.append({"model": name, "batch_size": batch, "median_batch_ms": round(median, 1),
                         "ms_per_image": round(median / batch, 1)})
            print(rows[-1])
    save("model_only", {"threads": 1, "image_size": "1600x1200", "rows": rows})


def scenario_render(runs: int = 5) -> None:
    """What each output costs on one core, after the model has made the mask."""
    from snaplist import imaging, pipeline
    segmenter = Segmenter(MODEL_SPECS["u2netp"], ROOT / "models", threads=1)
    timings: dict[str, list[float]] = {}
    sizes: dict[str, list[int]] = {}
    for seed in range(runs):
        image = pipeline.prepare(photo_bytes(seed=50000 + seed), max_side=2048)
        mask = imaging.clean_mask(segmenter.predict([image])[0])
        result = pipeline.render(image, mask, pipeline.EXTRA_OUTPUTS)
        for stage, ms in result.timings_ms.items():
            timings.setdefault(stage, []).append(ms)
        for name, data in result.files.items():
            sizes.setdefault(name, []).append(len(data))
    save("render_stages", {
        "runs": runs, "photo_size": "1600x1200", "threads": 1,
        "median_ms": {stage: percentile(values, 50) for stage, values in timings.items()},
        "median_output_kb": {name: round(statistics.median(values) / 1024) for name, values in sizes.items()},
    })


def scenario_throughput(job_count: int = 120) -> None:
    """End-to-end: a burst of uploads, processed by 1, 2 and 3 workers (machine has 2 vCPUs)."""
    runs = []
    for workers, outputs in ((1, ""), (2, ""), (3, ""), (2, "studio")):
        batch = photos(job_count, offset=1000 * workers + (500 if outputs else 0))
        with Stack() as stack:
            stack.start_workers(workers)
            uploads = asyncio.run(submit_all(stack.url, batch, concurrency=16, outputs=outputs))
            wait_until_idle(stack)
            result = analyse(job_records(stack, [u["job_id"] for u in uploads]))
        result = {"workers": workers, "outputs": "main" + (f"+{outputs}" if outputs else ""),
                  "upload_ms": summary([u["ms"] for u in uploads])} | result
        runs.append(result)
        print(json.dumps(result))
    save("throughput", {"jobs_per_run": job_count, "photo_size": "1600x1200 JPEG", "runs": runs})


async def submit_steady(url: str, photos_: list[bytes], per_minute: float) -> list[dict]:
    """Open-loop load: one upload every 60/per_minute seconds, whatever the server is doing."""
    interval = 60.0 / per_minute
    results: list[dict] = []
    started = time.perf_counter()

    async def one(session: aiohttp.ClientSession, index: int, photo: bytes) -> None:
        await asyncio.sleep(max(0.0, started + index * interval - time.perf_counter()))
        form = aiohttp.FormData()
        form.add_field("image", photo, filename=f"s{index}.jpg", content_type="image/jpeg")
        sent = time.perf_counter()
        async with session.post(f"{url}/v1/jobs", data=form, headers={"X-Seller-Id": "steady"}) as response:
            body = await response.json()
            results.append({"status": response.status, "ms": (time.perf_counter() - sent) * 1000,
                            "job_id": body.get("job_id")})

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(one(session, i, p) for i, p in enumerate(photos_)))
    return results


def scenario_steady(per_minute: float | None = None, seconds: int = 120, workers: int = 2) -> None:
    """Uploads arrive at a steady rate below capacity: the wait a seller sees under normal load."""
    if per_minute is None:  # 70% of the measured capacity of 2 workers, main image only
        runs = json.loads((RESULTS / "throughput.json").read_text())["runs"]
        capacity = next(r["images_per_minute"] for r in runs if r["workers"] == workers and r["outputs"] == "main")
        per_minute = round(capacity * 0.7)
    count = int(per_minute * seconds / 60)
    with Stack() as stack:
        stack.start_workers(workers)
        uploads = asyncio.run(submit_steady(stack.url, photos(count, offset=30000), per_minute))
        wait_until_idle(stack)
        jobs = job_records(stack, [u["job_id"] for u in uploads])
    save("steady_load", {
        "workers": workers, "arrival_per_minute": per_minute, "duration_s": seconds,
        "upload_ms": summary([u["ms"] for u in uploads]), "jobs": analyse(jobs),
    })


def scenario_chaos(job_count: int = 120, kills: int = 4, every_s: float = 8.0,
                   visibility_timeout_ms: int = 5000) -> None:
    """Kill a busy worker with SIGKILL every few seconds, and start a replacement each time
    (what an orchestrator does when a container dies). No job may be lost."""
    with Stack(visibility_timeout_ms=visibility_timeout_ms) as stack:
        stack.start_workers(2)
        uploads = asyncio.run(submit_all(stack.url, photos(job_count, offset=9000), concurrency=16))
        kill_times = []
        for _ in range(kills):
            time.sleep(every_s)
            victim = next(i for i, w in enumerate(stack.workers) if w.poll() is None)
            kill_times.append(time.time() * 1000)
            stack.kill_worker(victim)
            stack.start_workers(1)  # the replacement
        wait_until_idle(stack)
        jobs = job_records(stack, [u["job_id"] for u in uploads])
        pending = stack.stats()["tiers"]["standard"]["pending_unacknowledged"]
    first_upload = min(j["created_at_ms"] for j in jobs)
    taken_over = [j for j in jobs if j["attempts"] > 1]
    result = analyse(jobs) | {
        "visibility_timeout_ms": visibility_timeout_ms,
        "workers_killed": kills,
        "kill_times_after_first_upload_s": [round((t - first_upload) / 1000, 1) for t in kill_times],
        "jobs_taken_over": len(taken_over),
        "taken_over_end_to_end_ms": summary([j["total_ms"] for j in taken_over]),
        "lost_jobs": job_count - sum(j["state"] in {"succeeded", "failed"} for j in jobs),
        "pending_after_run": pending,
    }
    save("chaos_kill_worker", result)


def scenario_overload(burst: int = 300, max_open: int = 50) -> None:
    """More uploads than the queue limit: excess requests get a fast 503."""
    with Stack(max_open_jobs=max_open) as stack:
        stack.start_workers(1)
        uploads = asyncio.run(submit_all(stack.url, photos(burst, offset=20000, size=(1000, 750)), concurrency=32))
        accepted = [u for u in uploads if u["status"] == 202]
        rejected = [u for u in uploads if u["status"] == 503]
        wait_until_idle(stack)
        jobs = job_records(stack, [u["job_id"] for u in accepted])
    save("overload", {
        "burst": burst, "max_open_jobs": max_open, "workers": 1,
        "accepted": len(accepted), "rejected_503": len(rejected),
        "other_status": sorted({u["status"] for u in uploads} - {202, 503}),
        "accepted_upload_ms": summary([u["ms"] for u in accepted]),
        "rejected_upload_ms": summary([u["ms"] for u in rejected]),
        "accepted_jobs": analyse(jobs),
    })


def scenario_api(uploads: int = 400) -> None:
    """How fast the API alone accepts uploads (no workers, so nothing competes for CPU)."""
    batch = photos(uploads, offset=40000)
    with Stack(max_open_jobs=100000) as stack:
        started = time.perf_counter()
        results = asyncio.run(submit_all(stack.url, batch, concurrency=32))
        elapsed = time.perf_counter() - started
    save("api_accept", {
        "uploads": uploads, "concurrency": 32, "mean_photo_kb": round(sum(map(len, batch)) / len(batch) / 1024),
        "accepted": sum(r["status"] == 202 for r in results),
        "uploads_per_second": round(uploads / elapsed, 1),
        "accept_ms": summary([r["ms"] for r in results]),
    })


SCENARIOS = {"model": scenario_model, "render": scenario_render, "throughput": scenario_throughput,
             "steady": scenario_steady,
             "chaos": scenario_chaos, "overload": scenario_overload, "api": scenario_api}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="+", choices=[*SCENARIOS, "all"])
    args = parser.parse_args()
    for name, run in SCENARIOS.items():
        if name in args.scenarios or "all" in args.scenarios:
            print(f"=== {name} ===", flush=True)
            run()
