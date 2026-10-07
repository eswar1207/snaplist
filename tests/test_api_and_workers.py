"""End-to-end behaviour with a real Redis server and real workers."""

import time
from concurrent.futures import ThreadPoolExecutor

from snaplist.jobs import DLQ_STREAM
from snaplist.ratelimit import TokenBucketLimiter
from tests.conftest import wait_for_state
from tests.photos import photo_bytes


def submit(client, data: bytes, seller="seller-1", outputs="", key=None, fault=None, tier="standard"):
    headers = {"X-Seller-Id": seller}
    if key:
        headers["Idempotency-Key"] = key
    form = {"tier": tier, "outputs": outputs}
    if fault:
        form["fault"] = fault
    return client.post("/v1/jobs", headers=headers, data=form, files={"image": ("photo.jpg", data, "image/jpeg")})


def test_job_runs_end_to_end(make_client, run_worker):
    client = make_client()
    run_worker()
    response = submit(client, photo_bytes(seed=1), outputs="studio")
    assert response.status_code == 202
    assert response.headers["Location"] == f"/v1/jobs/{response.json()['job_id']}"

    job = wait_for_state(client, response.json()["job_id"], {"succeeded", "failed"})
    assert job["state"] == "succeeded", job
    assert job["compliance"]["passed"]
    assert set(job["outputs"]) == {"main.jpg", "studio-grey.jpg", "studio-warm.jpg"}
    image = client.get(job["outputs"]["main.jpg"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    assert client.get("/v1/stats").json()["tiers"]["standard"]["open_jobs"] == 0


def test_same_photo_twice_reuses_the_first_job(make_client):
    client = make_client()
    first = submit(client, photo_bytes(seed=2))
    again = submit(client, photo_bytes(seed=2))
    other_seller = submit(client, photo_bytes(seed=2), seller="seller-2")
    assert first.status_code == 202
    assert again.status_code == 200 and again.json()["job_id"] == first.json()["job_id"]
    assert again.headers["X-SnapList-Reused"] == "duplicate_upload"
    assert other_seller.status_code == 202  # sellers never share each other's results


def test_idempotency_key_replay_and_conflict(make_client):
    client = make_client()
    first = submit(client, photo_bytes(seed=3), key="order-abc-123")
    replay = submit(client, photo_bytes(seed=3), key="order-abc-123")
    conflict = submit(client, photo_bytes(seed=4), key="order-abc-123")
    assert replay.status_code == 200 and replay.json()["job_id"] == first.json()["job_id"]
    assert conflict.status_code == 422


def test_concurrent_identical_requests_create_exactly_one_job(make_client, redis_client):
    client = make_client()
    data = photo_bytes(seed=5)
    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(lambda _: submit(client, data, key="retry-storm-01"), range(10)))
    job_ids = {r.json()["job_id"] for r in responses}
    assert len(job_ids) == 1
    assert sorted(r.status_code for r in responses).count(202) == 1
    assert int(redis_client.get("snaplist:open:standard")) == 1


def test_bad_uploads_are_rejected(make_client):
    client = make_client(max_upload_bytes=50_000)
    not_an_image = client.post("/v1/jobs", headers={"X-Seller-Id": "s1"},
                               files={"image": ("notes.txt", b"hello", "text/plain")})
    too_big = submit(client, photo_bytes(seed=6, width=1600, height=1200))
    bad_seller = submit(client, photo_bytes(seed=6), seller="not valid!")
    assert not_an_image.status_code == 400
    assert too_big.status_code == 413
    assert bad_seller.status_code == 400


def test_rate_limit_per_seller(make_client):
    client = make_client(rate_limit_burst=3, rate_limit_per_minute=1)
    codes = [submit(client, photo_bytes(seed=10 + i)).status_code for i in range(4)]
    assert codes == [202, 202, 202, 429]
    limited = submit(client, photo_bytes(seed=20))
    assert int(limited.headers["Retry-After"]) >= 1
    assert submit(client, photo_bytes(seed=21), seller="another-seller").status_code == 202


def test_token_bucket_refills(redis_client):
    limiter = TokenBucketLimiter(redis_client, burst=2, per_minute=60_000)  # 1000 tokens/second
    assert limiter.check("s").allowed and limiter.check("s").allowed
    assert not limiter.check("s").allowed
    time.sleep(0.01)
    assert limiter.check("s").allowed


def test_load_shedding_when_the_queue_is_full(make_client):
    client = make_client(max_open_jobs_per_tier=2)  # no worker running, so jobs stay open
    codes = [submit(client, photo_bytes(seed=30 + i)).status_code for i in range(3)]
    assert codes == [202, 202, 503]


def test_temporary_failure_is_retried(make_client, run_worker):
    client = make_client()
    run_worker()
    job_id = submit(client, photo_bytes(seed=40), fault="transient:1").json()["job_id"]
    job = wait_for_state(client, job_id, {"succeeded", "failed"})
    assert job["state"] == "succeeded"
    assert job["attempts"] == 2
    assert "injected failure" in job["last_error"]


def test_job_goes_to_dead_letter_queue_after_max_attempts(make_client, run_worker, redis_client):
    client = make_client()
    run_worker(max_attempts=2)
    job_id = submit(client, photo_bytes(seed=41), fault="transient:9").json()["job_id"]
    job = wait_for_state(client, job_id, {"succeeded", "failed"})
    assert job["state"] == "failed" and job["attempts"] == 2
    assert "gave up after 2 attempts" in job["error"]
    entries = redis_client.xrange(DLQ_STREAM)
    assert [e[1][b"job_id"].decode() for e in entries] == [job_id]
    assert client.get("/v1/stats").json()["tiers"]["standard"]["open_jobs"] == 0


def test_crashed_worker_job_is_taken_over(make_client, spawn_worker_process):
    client = make_client()
    doomed = spawn_worker_process("doomed-worker")
    job_id = submit(client, photo_bytes(seed=42), fault="crash").json()["job_id"]
    assert doomed.wait(timeout=60) == 17  # the worker died in the middle of the job
    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "processing"

    spawn_worker_process("rescue-worker")
    job = wait_for_state(client, job_id, {"succeeded", "failed"}, timeout=60)
    assert job["state"] == "succeeded"
    assert job["attempts"] == 2
    assert client.get("/v1/stats").json()["tiers"]["standard"]["pending_unacknowledged"] == 0


def test_health_and_metrics(make_client):
    client = make_client()
    assert client.get("/health").json() == {"status": "ok", "redis": True}
    assert "snaplist_jobs_submitted_total" in client.get("/metrics").text
