# SnapList — interview preparation

Read [DESIGN.md](DESIGN.md) first. This page is what to *say*.
All numbers are from [BENCHMARKS.md](BENCHMARKS.md). If you are not sure of a
number in the interview, say "about" and explain how you measured it. Never
guess a number.

## 1. The 30-second pitch

> Small sellers take product photos on a phone, but Amazon's main image needs a
> pure white background, the product filling 85% of the frame and at least
> 1000 px. I built SnapList, a backend that turns one phone photo into an
> Amazon-ready main image, studio shots, a cutout and a short B-roll video.
> Uploads are accepted in milliseconds and processed by workers through a
> Redis Streams queue, with at-least-once delivery, idempotent uploads, per-seller
> rate limits and load shedding. I measured everything: one CPU worker does
> about ⟨82⟩ images a minute, and when I kill a worker in the middle of a run,
> no job is lost.

## 2. The 2-minute walk-through (draw this on the whiteboard)

1. **Client → API.** `POST /v1/jobs` with the photo. The API does only cheap work: validate, rate-limit (token bucket in Redis), check the image header, fingerprint the request, dedupe, check the queue is not full, store the original, enqueue. It answers `202 Accepted` with a job URL.
2. **Queue.** One Redis stream per quality tier, with a consumer group. Two tiers because the models differ 6× in cost; separate queues stop slow jobs from blocking fast ones.
3. **Worker.** Reads a job with `XREADGROUP`, runs the background-removal model (U²-Net-p in ONNX Runtime), cleans the mask, places the product on a 2000 px white canvas at 85% fill, encodes the JPEG, and checks the rules **on the encoded JPEG**. Writes outputs, marks the job final with a Lua script, then `XACK`.
4. **Failures.** If a worker dies, the job stays in the pending list and another worker takes it over with `XAUTOCLAIM` after the visibility timeout. Temporary errors retry up to 3 times, then go to a dead-letter queue.
5. **Scale.** API is stateless. Workers are one per CPU core and autoscale on queue depth. In AWS terms: S3 for images, SQS instead of Redis Streams, ECS or EKS for workers, CloudWatch alarms on queue age.

## 3. Numbers to remember

| What | Number |
| --- | --- |
| Model alone, standard tier (U²-Net-p, 1 core) | ≈ 470 ms per image |
| Model alone, high tier (IS-Net, 1 core) | ≈ 3,000 ms per image (6.4× slower) |
| Batching on CPU | no gain (470 ms alone vs 476–526 ms per image in batches) |
| One worker, main image only | ⟨82⟩ images/min |
| Two workers (2 cores) | ⟨…⟩ images/min |
| Three workers on 2 cores | ⟨…⟩ images/min — no gain, CPU-bound |
| Time per job inside a worker | decode ⟨21⟩ ms, model ⟨492⟩ ms, render ⟨192⟩ ms, store ⟨1⟩ ms |
| Upload accepted (API alone) | p50 ⟨…⟩ ms, ⟨…⟩ uploads/s |
| Worker killed mid-run | ⟨…⟩ jobs taken over, 0 lost |
| Overload (300 uploads, queue limit 50) | ⟨…⟩ accepted, ⟨…⟩ fast `503`s |
| Tests | 26, against a real Redis server and real worker processes |

## 4. Questions you will likely get

### About the problem

**Q1. Why did you build this?**
Many small sellers in India list products with phone photos. Amazon rejects or
suppresses main images that break the rules, and editing by hand is slow. I
wanted a project where the system design matters (bursty uploads, slow CPU
work, failures) and the result is easy to check (the rules are measurable).

**Q2. What exactly does "compliant" mean in your code?**
Five checks on the final JPEG: background pixels are exactly RGB 255 (at least
99.9% of them, ignoring an 8 px band next to the product), product's longest
side is 85% of the frame, image at least 1000 px, square, and the product does
not touch the edge.

### About the API

**Q3. Why `202 Accepted` and not `200`?**
The work is not done yet. `202` says "accepted for processing", and the
`Location` header tells the client where to poll.

**Q4. A seller's phone loses network after sending the upload and retries. What happens?**
The retry has the same photo, so the same fingerprint. The dedupe key (set with
`SET NX`) points to the first job, and the API returns that job with `200`. If
the client also sends an `Idempotency-Key`, the replay is matched by the key.
A test sends 10 identical requests at the same time and checks that exactly
one job is created.

**Q5. What is the difference between dedupe and the idempotency key?**
Dedupe is automatic: same seller + same photo + same options = same job. The
idempotency key is the client's explicit promise: "this is the same request".
If the same key comes with a *different* photo, that is a client bug, so I
return `422` instead of silently doing something.

**Q6. How does the rate limiter work?**
Token bucket per seller in a Redis Lua script: `burst` tokens, refilled at
`per_minute / 60` per second. The script reads the bucket, adds tokens for the
time passed, takes one if it can, and writes back, all atomically. It uses
Redis's own clock (`TIME`), so API servers with different clocks agree. When
empty: `429` with `Retry-After`.

**Q7. Why check the image header in the API but decode it in the worker?**
Decoding a 50 MP photo costs CPU and memory. The API should stay cheap and
fast. Reading only the header still catches wrong formats and tiny images.

### About the queue and reliability

**Q8. Why Redis Streams and not Kafka?**
This is a task queue: each job goes to one worker and is acknowledged one by
one. Streams with consumer groups give exactly that: a pending list per
consumer, `XACK`, and `XAUTOCLAIM` to take over stuck jobs. Kafka tracks one
offset per partition, so one slow job holds up its partition, and there is no
per-message visibility timeout. Kafka is great for event logs and replay.

**Q9. Why not SQS?**
On AWS I would use SQS. It has visibility timeouts and dead-letter queues built
in. I wanted the project to run on a laptop, and the design maps one-to-one:
`XAUTOCLAIM` after the visibility timeout is SQS's visibility timeout, and my
DLQ stream is SQS's redrive policy.

**Q10. What delivery guarantee do you give?**
At-least-once. A job is acknowledged only after its outputs are stored and its
record is final. If the worker dies before `XACK`, another worker processes it
again. Exactly-once is not possible in general, so I made repeats harmless.

**Q11. How did you make repeats harmless?**
Outputs go to fixed file names with write-to-temp-then-rename, so a rerun
writes the same files. The final state is set by a Lua script that does
nothing if the job is already final, and it decrements the open-jobs counter
in the same atomic step, so nothing is counted twice.

**Q12. What is a visibility timeout and how did you choose 30 seconds?**
It is how long a job can stay unacknowledged before another worker may take
it. Too short: slow but healthy jobs get processed twice. Too long: a crashed
worker's job waits a long time. A standard job takes under 1 s and a
high-quality job about 3–4 s, so 30 s gives a big safety margin. In the chaos
benchmark I used 5 s to make the takeover visible.

**Q13. What happens with a "poison" photo that always crashes the worker?**
Each delivery increments `attempts`. After 3 attempts the job is marked failed
and written to the dead-letter stream. A photo that simply cannot be decoded
fails at once, because retrying cannot help.

**Q14. How did you test that a crashed worker's job is not lost?**
Two ways. A test starts a real worker process with a fault flag that makes it
call `os._exit(17)` in the middle of a job, checks the job is stuck in
"processing", then starts a second worker and checks the job succeeds on
attempt 2 with nothing left pending. And the chaos benchmark kills one of two
workers with `SIGKILL` during a run of 80 jobs: ⟨…⟩ jobs were taken over and 0
were lost.

**Q15. How does load shedding work?**
An open-jobs counter per tier: `INCR` in the same transaction that enqueues,
`DECR` in the finish script. If it is at the limit, the API answers `503` with
`Retry-After` before storing anything. In the overload benchmark, ⟨…⟩ of 300
uploads were rejected in about ⟨…⟩ ms each, and every accepted job finished.

**Q16. Isn't there a race between checking the counter and incrementing it?**
Yes, a small one: two API servers can both see 499 and both enqueue, so the
queue can go a little over the limit. That is fine for a soft limit. If it had
to be exact, I would move the check and the increment into one Lua script.

### About scaling

**Q17. How do you handle 10× traffic?**
Add API servers (stateless) and add workers. Throughput grows with CPU cores:
⟨…⟩ images/min with 1 worker, ⟨…⟩ with 2 on 2 cores, but 3 workers on 2 cores
gave no more. So: one worker per core, and autoscale on queue depth.

**Q18. Autoscale on what metric?**
Expected wait = open jobs ÷ (workers × images per worker per minute). If it
goes above the target (say 30 s), add workers. CPU usage is a bad signal
here: a busy worker is always at 100%, whether the queue is empty or huge.

**Q19. What breaks first at 100×?**
Moving photo bytes through the API servers. I would let sellers upload
straight to S3 with a pre-signed URL and send only the key to the API. Next,
Redis: each job is about ⟨15–20⟩ Redis commands, so one instance is fine to
thousands of jobs per second; after that, shard the streams by seller or move
the queue to SQS.

**Q20. Would a GPU help?**
For the high tier, yes: IS-Net is 3 s per image on a CPU core. On a GPU,
batching would also start to pay off, and my worker already supports dynamic
batching (it waits up to 20 ms to fill a batch).

**Q21. How would you store and serve the images in production?**
S3 for originals and outputs, with a lifecycle rule to delete originals after
some days, and CloudFront in front of the outputs. My storage class has the
same small interface (put, get, exists, delete), so only that class changes.

### About performance

**Q22. You said batching did not help. Why?**
I measured it: 470 ms per image alone, 476–526 ms per image in batches of 2 to
8. A convolution network on one CPU core is compute-bound: the core is already
busy, so putting more images in one call does the same work and only adds
waiting. On a GPU, one image does not fill the hardware, so batching helps.
I kept the code but set `batch_size = 1`.

**Q23. Where does the time go in one job?**
Model ⟨≈ 490⟩ ms, rendering ⟨≈ 190⟩ ms (compose, JPEG encode, decode for the
check), decode ⟨≈ 20⟩ ms, storing ⟨≈ 1⟩ ms. The model is about 70%.

**Q24. What did you optimise?**
Three things, each measured: (1) shrink photos to 2048 px right after
decoding, (2) blend only the product's area instead of the full 2000 × 2000
canvas, (3) cache the studio backgrounds per worker because they never change.

**Q25. Why is your end-to-end time in the burst test so long?**
Because 120 uploads arrive in about 2 seconds and one worker does ⟨82⟩ a
minute. The last job waits about a minute and a half in the queue. Processing
time per job is under a second; the rest is queue wait. In the steady-load
test, at 70% of capacity, the p95 is ⟨…⟩ s. That is why the autoscaling
metric is queue wait, not CPU.

### About the image and the model

**Q26. Which model and why?**
U²-Net-p for the standard tier: 4.6 MB, fast on CPU. IS-Net for the high tier:
179 MB, cleaner edges (hair, fur), 6× slower. Both Apache-2.0 licensed. I run
them with ONNX Runtime so the same file works on CPU or GPU, on Linux or
Windows, without PyTorch.

**Q27. What was the hardest bug?**
Main images failed the white-background check even though they looked white.
I measured the pixels: the model leaves faint alpha (1–19 out of 255) far from
the product, and JPEG compression changes pixels next to the product. I fixed
it by cleaning the mask (keep the biggest object, zero alpha below 20), saving
JPEG with full colour resolution (4:4:4), and checking the JPEG *after*
encoding, ignoring an 8 px band next to the product. After that, all test
photos passed.

**Q28. How did you make the model accept batches?**
The exported ONNX file had a fixed batch size of 1. I rewrote the graph's input
and output batch dimension into a named dynamic dimension with the `onnx`
library, and a test checks batched results equal single results (difference
at most 1 grey level, from rounding).

**Q29. Phone photos are often sideways. How do you handle that?**
Phones store the rotation in EXIF instead of rotating the pixels. The decoder
applies the EXIF orientation first. There is a test for it.

### About testing and quality

**Q30. How did you test it?**
26 tests with pytest. Unit tests for image steps. Integration tests start a
real `redis-server` and real worker processes: end-to-end job, dedupe,
idempotency replay and conflict, 10 concurrent identical uploads, bad uploads,
rate limit, load shedding, retry, dead-letter queue, crashed worker takeover.
No Redis mocks, because the bugs I worry about live in Redis behaviour.

**Q31. How do you know the system works in production?**
Prometheus metrics: jobs submitted, rejected (by reason), reused, finished (by
state), retries, takeovers, time per stage, total job time. I would alarm on
queue wait (oldest job age), DLQ growth and the failure rate.

### About trade-offs and next steps

**Q32. What would you change if you rebuilt it?**
Upload straight to S3 with pre-signed URLs; fair scheduling between sellers
inside a tier; a webhook when the job finishes instead of polling; a quality
benchmark on a labelled dataset with IoU.

**Q33. What are its limits today?**
Benchmarks use synthetic photos on a 2-vCPU machine. Storage is local disk.
The B-roll is a zoom-and-pan, not generative video.

**Q34. Did you use AI tools?**
Answer honestly. For example: "Yes, I used an AI coding assistant to write
code faster. I chose the design, I reviewed the code, and I checked it with
tests and benchmarks; for example, the benchmark is what showed me batching
does not help on CPU." Then be ready to explain any file line by line.

## 5. Leadership Principles stories from this project

| Principle | Story (keep it to STAR: situation, task, action, result) |
| --- | --- |
| Customer Obsession | Built for small sellers: one phone photo in, Amazon-ready images out, with a clear report of which rule failed. |
| Dive Deep | Off-white backgrounds: measured pixel values, found model haze and JPEG ringing, fixed both, all samples passed. |
| Insist on the Highest Standards | Check the JPEG that Amazon receives, not the clean image in memory. Chaos test with `SIGKILL`, not just happy paths. |
| Frugality | CPU-only by default, small 4.6 MB model for the standard tier, batching switched off after measuring that it costs more. |
| Invent and Simplify | Redis does queue, rate limit and dedupe, so one system to run instead of three. |
| Are Right, A Lot | Expected batching to help; measured it; it did not; changed the default and wrote down why. |

## 6. Study plan before the interview

1. Run it: `scripts/run.sh`, open <http://127.0.0.1:8300>, upload a photo of something on your table.
2. Read the code in this order: `config.py` → `api.py` → `jobs.py` → `worker.py` → `model.py` → `imaging.py` → `pipeline.py` → `tests/`.
3. Break it on purpose: start two workers, kill one in the middle (`kill -9`), and watch `/v1/stats` and the job record (`attempts` becomes 2).
4. Re-run one benchmark yourself, so the numbers are yours.
5. Practise the 2-minute walk-through out loud, drawing the diagram.
