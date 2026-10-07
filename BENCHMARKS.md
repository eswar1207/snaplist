# SnapList benchmarks

Every number below comes from `bench/run_benchmarks.py`, and this file is
written by `bench/make_report.py` from the JSON in
[`benchmarks/results/`](benchmarks/results). Nothing is typed by hand.

**Setup.** A cloud machine with 2 vCPUs and 7.8 GB RAM, Python 3.13.16.
Each scenario starts real processes: `redis-server`, the API under uvicorn, and
worker processes, each using one thread (`OMP_NUM_THREADS=1`). Photos are
synthetic 1600 × 1200 JPEG product photos made by `tests/photos.py`. The
timings come from the job records that the workers write.

Reproduce: `python bench/run_benchmarks.py all && python bench/make_report.py`

## 1. The model alone: does batching help?

One worker thread, 1600x1200 photos, median of several runs.

| Model | Images per call | Time per call | Time per image |
| --- | --- | --- | --- |
| u2netp | 1 | 473 ms | 473 ms |
| u2netp | 2 | 974 ms | 487 ms |
| u2netp | 4 | 1,942 ms | 486 ms |
| u2netp | 8 | 3,822 ms | 478 ms |
| isnet-general-use | 1 | 3,000 ms | 3,000 ms |
| isnet-general-use | 2 | 5,899 ms | 2,949 ms |

**Finding:** no. U²-Net-p takes 473 ms per image alone and
478–487 ms per image in batches. A convolution network
already keeps one CPU core fully busy, so a batch only does the same work in
one bigger call. SnapList therefore uses `batch_size = 1` on CPU. IS-Net (high
tier) is 6.3× slower than U²-Net-p,
which is why each tier has its own queue.

## 2. What each output costs

After the model has made the mask. One thread, median of 5 photos.

| Output | Median time |
| --- | --- |
| Main image (compose 2000 px, JPEG, rule check) | 149 ms |
| Cutout PNG | 10 ms |
| Two studio shots | 191 ms |
| 5 s B-roll video (120 frames, H.264) | 1,641 ms |

Median file sizes: `main.jpg` 249 KB, `cutout.png` 182 KB, `studio-grey.jpg` 211 KB, `studio-warm.jpg` 213 KB, `broll.mp4` 124 KB.

The video is the most expensive output, so it is opt-in (`outputs=video`).

## 3. Throughput: a burst of 120 uploads

All 120 photos (1600x1200 JPEG) are uploaded at once (16 at a time), then the
workers drain the queue. The machine has 2 vCPUs. "Work per job" is the
median time a worker spends on one job (decode + model + render + store).

| Workers | Outputs | Images / min | Work per job | Model | End-to-end p50 | End-to-end p95 | Passed rules |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | main | **87** | 676 ms | 479 ms | 41.4 s | 78.3 s | 120/120 |
| 2 | main | **161** | 716 ms | 495 ms | 22.4 s | 41.3 s | 120/120 |
| 3 | main | **159** | 1,092 ms | 782 ms | 23.5 s | 42.6 s | 120/120 |
| 2 | main+studio | **123** | 956 ms | 506 ms | 29.7 s | 55.0 s | 120/120 |

**Findings**

- Two workers on two cores give 1.86× the throughput of one (87 → 161 images/min).
- A third worker on the same two cores gives 159 images/min: no gain, because the work is CPU-bound. Rule: one worker per core, scale out with more machines.
- End-to-end time in a burst is almost all queue wait: work per job is under a second, but the last of 120 jobs waits for all the others. This is why the autoscaling signal should be queue wait, not CPU.

## 4. Steady load (normal traffic)

Uploads arrive at a steady 113 per minute for 120 s
(about 70% of what 2 workers can do), main image only.

| Jobs | End-to-end p50 | p95 | p99 | Queue wait p50 | Queue wait p95 | Upload p50 |
| --- | --- | --- | --- | --- | --- | --- |
| 226 | 725 ms | 811 ms | 846 ms | 1 ms | 3 ms | 9 ms |

With spare capacity there is almost no queueing: 99% of sellers get the main
image back within 846 ms of uploading.

## 5. Killing workers in the middle of a run

Two workers, 120 jobs uploaded at once, visibility timeout 5 s.
A busy worker is killed with `SIGKILL` (no clean-up, like a machine dying)
4 times, at 9 s, 18 s, 26 s, 35 s after the first upload, and a replacement
worker is started each time (what an orchestrator does when a container dies).

| Jobs | Succeeded | Failed | Lost | Taken over after a kill | Still pending after the run | Images / min |
| --- | --- | --- | --- | --- | --- | --- |
| 120 | 120 | 0 | 0 | 4 | 0 | 156 |

The 4 jobs that were in progress on the killed workers (a worker takes one
job at a time) stayed in the Redis Streams pending list. After the visibility
timeout another worker claimed each of them with `XAUTOCLAIM` and finished it
on attempt 2. Nothing was lost and nothing was left pending.

## 6. Overload: more uploads than the queue limit

300 uploads at once (32 at a time), queue limit 50 open jobs, 1 worker.

| Accepted (202) | Rejected (503) | Other status | Accepted upload p50 | Rejected upload p50 | Accepted jobs finished |
| --- | --- | --- | --- | --- | --- |
| 50 | 250 | none | 182 ms | 76 ms | 50/50 |

Rejected requests get a quick `503` with `Retry-After: 5` instead of waiting in
an ever-growing queue, and every accepted job still finishes.

The first version of this benchmark found a bug: with the same settings it
accepted **62 jobs against a limit of 50**, because the API checked the
counter and incremented it in two separate steps. Now both happen in one Lua
script (see DESIGN.md, 5.11). The old result is kept in
`benchmarks/results/history/`.

## 7. The API alone

400 uploads (mean photo 375 KB), 32 at a time, no workers running.

| Accepted | Uploads / second | p50 | p95 | p99 |
| --- | --- | --- | --- | --- |
| 400 | 198.6 | 154 ms | 239 ms | 297 ms |

One API process (uvicorn, one event loop, sync endpoints in its thread pool)
on the same 2-vCPU machine as the load generator.

## Limits of these numbers

- Synthetic photos, one machine, 2 vCPUs. Real phone photos are bigger; they are shrunk to 2048 px first, so the work per job stays close to this.
- The load generator runs on the same machine as the system, so it takes some CPU too.
- Storage is the local disk; with S3 the store step would take longer, but it is not on the critical CPU path.
