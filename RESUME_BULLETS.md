# SnapList — resume lines

Every number below is in [BENCHMARKS.md](BENCHMARKS.md) or comes from the test
suite. Do not change a number without re-running the benchmark.

## Heading

**SnapList — Phone Photo to Amazon Listing Images** | Python, FastAPI, Redis Streams, ONNX Runtime, OpenCV, Docker | github.com/eswar1207/snaplist

## On the resume now (one line each, at most 99 characters)

- Built a FastAPI service that turns seller phone photos into Amazon-compliant 2000 px main images.
- Processed 161 images/min on 2 CPU cores with ONNX Runtime; p99 846 ms at 70% load, no GPU.
- Made the Redis Streams job queue fault-tolerant: killed 4 workers mid-run and lost 0 of 120 jobs.
- Found 2 race conditions in load tests (62 jobs let in past a limit of 50); fixed with atomic Lua.

## Longer versions (two lines each)

- Built a backend that turns a seller's phone photo into an Amazon-compliant 2000 px main image, studio shots and B-roll; 161 images/min on 2 CPU cores, p99 846 ms at 70% load.
- Designed per-tier Redis Streams queues with at-least-once delivery, XAUTOCLAIM takeover and a dead-letter queue; killed 4 workers mid-run (SIGKILL) and lost 0 of 120 jobs.
- Made uploads idempotent (SET NX GET leases, Idempotency-Key) and load shedding exact with Lua; found both races with 30 real-Redis tests and benchmarks (62 accepted vs limit 50).

## Spare bullets (swap one in if a job post stresses it)

- Measured batched ONNX inference on CPU: no gain (473 ms vs 478–487 ms per image), so batch size stays 1; dynamic batching kept for GPUs.
- Checked Amazon's image rules on the encoded JPEG; fixed off-white backgrounds caused by model haze and JPEG ringing; 876/876 images passed.
- Per-seller token-bucket rate limit in Lua on Redis server time, 503 + Retry-After load shedding, Prometheus metrics for every stage.

## LaTeX (the macros used in the Overleaf resume)

```latex
\resumeProjectHeading
  {\href{https://github.com/eswar1207/snaplist}{\textbf{\large{\underline{SnapList}}}} $|$ \large{\underline{Python, FastAPI, Redis Streams, ONNX Runtime, OpenCV, Docker}}}{2026}\\
  \resumeItemListStart
    \resumeItem{\normalsize{Built a FastAPI service that turns seller phone photos into Amazon-compliant 2000\,px main images.}}
    \resumeItem{\normalsize{Processed 161 images/min on 2 CPU cores with ONNX Runtime; p99 846\,ms at 70\% load, no GPU.}}
    \resumeItem{\normalsize{Made the Redis Streams job queue fault-tolerant: killed 4 workers mid-run and lost 0 of 120 jobs.}}
    \resumeItem{\normalsize{Found 2 race conditions in load tests (62 jobs let in past a limit of 50); fixed with atomic Lua.}}
  \resumeItemListEnd
```
