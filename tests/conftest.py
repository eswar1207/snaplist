"""Shared fixtures: a real Redis server, settings, the API client and workers."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
import redis
from fastapi.testclient import TestClient

from snaplist.api import create_app
from snaplist.config import PROJECT_ROOT, Settings
from snaplist.model import MODEL_SPECS, Segmenter
from snaplist.worker import Worker


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def redis_url(tmp_path_factory) -> str:
    if shutil.which("redis-server") is None:
        pytest.skip("redis-server is not installed")
    port = _free_port()
    workdir = tmp_path_factory.mktemp("redis")
    process = subprocess.Popen(
        ["redis-server", "--port", str(port), "--save", "", "--appendonly", "no", "--dir", str(workdir)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"redis://127.0.0.1:{port}/0"
    client = redis.Redis.from_url(url)
    for _ in range(100):
        try:
            client.ping()
            break
        except redis.ConnectionError:
            time.sleep(0.05)
    yield url
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture
def redis_client(redis_url) -> redis.Redis:
    client = redis.Redis.from_url(redis_url)
    client.flushdb()
    return client


@pytest.fixture
def settings(redis_client, redis_url, tmp_path) -> Settings:
    return Settings(
        redis_url=redis_url,
        storage_dir=tmp_path / "data",
        model_dir=PROJECT_ROOT / "models",
        visibility_timeout_ms=1500,  # short, so retry and takeover tests run fast
        read_block_ms=200,
        rate_limit_burst=1000,
        rate_limit_per_minute=60_000,
        allow_fault_injection=True,
    )


@pytest.fixture(scope="session")
def segmenter() -> Segmenter:
    return Segmenter(MODEL_SPECS["u2netp"], PROJECT_ROOT / "models")


@pytest.fixture
def make_client(settings):
    clients = []

    def _make(**overrides) -> TestClient:
        client = TestClient(create_app(replace(settings, **overrides)))
        client.__enter__()  # runs the app's startup (creates the queues)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def run_worker(settings, segmenter):
    """Start an in-process worker thread (shares the loaded model to keep tests fast)."""
    started: list[tuple[Worker, threading.Thread]] = []

    def _run(worker_id: str = "test-worker", **overrides) -> Worker:
        worker = Worker(replace(settings, **overrides), "standard", worker_id,
                        redis.Redis.from_url(settings.redis_url), segmenter=segmenter)
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        started.append((worker, thread))
        return worker

    yield _run
    for worker, thread in started:
        worker.stop()
        thread.join(timeout=10)


@pytest.fixture
def spawn_worker_process(settings):
    """Start a real worker OS process (needed when a test kills or crashes a worker)."""
    processes: list[subprocess.Popen] = []

    def _spawn(worker_id: str) -> subprocess.Popen:
        env = {
            "SNAPLIST_REDIS_URL": settings.redis_url,
            "SNAPLIST_STORAGE_DIR": str(settings.storage_dir),
            "SNAPLIST_MODEL_DIR": str(settings.model_dir),
            "SNAPLIST_VISIBILITY_TIMEOUT_MS": str(settings.visibility_timeout_ms),
            "SNAPLIST_READ_BLOCK_MS": str(settings.read_block_ms),
            "SNAPLIST_ALLOW_FAULT_INJECTION": "1",
            "OMP_NUM_THREADS": "1",
            "PATH": "/usr/bin:/bin",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "snaplist.worker", "--tier", "standard", "--worker-id", worker_id],
            cwd=Path(PROJECT_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        return process

    yield _spawn
    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def wait_for_state(client: TestClient, job_id: str, states: set[str], timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["state"] in states:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not reach {states} within {timeout}s; last state {job['state']}")
