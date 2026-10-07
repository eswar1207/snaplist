# SnapList — resume lines

Every number below is in [BENCHMARKS.md](BENCHMARKS.md) or comes from the test
suite. Do not change a number without re-running the benchmark.

## Heading

**SnapList — Phone Photo to Amazon Listing Images** | Python, FastAPI, Redis Streams, ONNX Runtime, OpenCV, Docker | github.com/eswar1207/snaplist

## Three bullets (use these)

- Built a backend that turns a seller's phone photo into an Amazon-compliant 2000 px main image, studio shots and B-roll; 161 images/min on 2 CPU cores, p99 846 ms at 70% load.
- Designed per-tier Redis Streams queues with at-least-once delivery, XAUTOCLAIM takeover and a dead-letter queue; killed 4 workers mid-run (SIGKILL) and lost 0 of 120 jobs.
- Made uploads idempotent (SET NX GET leases, Idempotency-Key) and load shedding exact with Lua; found both races with 30 real-Redis tests and benchmarks (62 accepted vs limit 50).

## Spare bullets (swap one in if a job post stresses it)

- Measured batched ONNX inference on CPU: no gain (473 ms vs 478–487 ms per image), so batch size stays 1; dynamic batching kept for GPUs.
- Checked Amazon's image rules on the encoded JPEG; fixed off-white backgrounds caused by model haze and JPEG ringing; 876/876 images passed.
- Per-seller token-bucket rate limit in Lua on Redis server time, 503 + Retry-After load shedding, Prometheus metrics for every stage.

## LaTeX (same macros as the current resume)

```latex
\resumeProjectHeading
  {\textbf{\href{https://github.com/eswar1207/snaplist}{SnapList}} $|$ \emph{Python, FastAPI, Redis Streams, ONNX Runtime, OpenCV, Docker}}{}
  \resumeItemListStart
    \resumeItem{Built a backend that turns a seller's phone photo into an Amazon-compliant 2000\,px main image, studio shots and B-roll; 161 images/min on 2 CPU cores, p99 846\,ms at 70\% load.}
    \resumeItem{Designed per-tier Redis Streams queues with at-least-once delivery, XAUTOCLAIM takeover and a dead-letter queue; killed 4 workers mid-run (SIGKILL) and lost 0 of 120 jobs.}
    \resumeItem{Made uploads idempotent (SET NX GET leases, Idempotency-Key) and load shedding exact with Lua; found both races with 30 real-Redis tests and benchmarks (62 accepted vs limit 50).}
  \resumeItemListEnd
```
