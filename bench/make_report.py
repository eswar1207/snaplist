"""Write BENCHMARKS.md from the JSON files in benchmarks/results/.

The numbers are never typed by hand: run the benchmarks, then this script.

Usage:  python bench/make_report.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "benchmarks" / "results"


def load(name: str) -> dict | None:
    path = RESULTS / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def ms(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value / 1000:.1f} s" if value >= 10_000 else f"{value:,.0f} ms"


def table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def section_model(data: dict) -> str:
    rows = [[r["model"], r["batch_size"], ms(r["median_batch_ms"]), ms(r["ms_per_image"])] for r in data["rows"]]
    base = {r["model"]: r["ms_per_image"] for r in data["rows"] if r["batch_size"] == 1}
    batched = [r["ms_per_image"] for r in data["rows"] if r["model"] == "u2netp" and r["batch_size"] > 1]
    return f"""## 1. The model alone: does batching help?

One worker thread, {data['image_size']} photos, median of several runs.

{table(["Model", "Images per call", "Time per call", "Time per image"], rows)}

**Finding:** no. U²-Net-p takes {base['u2netp']:.0f} ms per image alone and
{min(batched):.0f}–{max(batched):.0f} ms per image in batches. A convolution network
already keeps one CPU core fully busy, so a batch only does the same work in
one bigger call. SnapList therefore uses `batch_size = 1` on CPU. IS-Net (high
tier) is {base['isnet-general-use'] / base['u2netp']:.1f}× slower than U²-Net-p,
which is why each tier has its own queue.
"""


def section_render(data: dict) -> str:
    names = {"main_ms": "Main image (compose 2000 px, JPEG, rule check)", "studio_ms": "Two studio shots",
             "cutout_ms": "Cutout PNG", "video_ms": "5 s B-roll video (120 frames, H.264)"}
    rows = [[names.get(stage, stage), ms(value)] for stage, value in data["median_ms"].items()]
    sizes = ", ".join(f"`{name}` {kb:,} KB" for name, kb in data["median_output_kb"].items())
    return f"""## 2. What each output costs

After the model has made the mask. One thread, median of {data['runs']} photos.

{table(["Output", "Median time"], rows)}

Median file sizes: {sizes}.

The video is the most expensive output, so it is opt-in (`outputs=video`).
"""


def section_throughput(data: dict) -> str:
    rows = []
    for run in data["runs"]:
        stages = run["stage_median_ms"]
        per_job = sum(stages.get(k, 0) for k in ("decode_ms", "model_ms", "render_ms", "store_ms"))
        rows.append([
            run["workers"], run["outputs"], f"**{run['images_per_minute']:.0f}**", ms(per_job),
            ms(stages.get("model_ms")), ms(run["end_to_end_ms"]["p50"]), ms(run["end_to_end_ms"]["p95"]),
            f"{run['compliance_passed']}/{run['succeeded']}",
        ])
    runs = {(r["workers"], r["outputs"]): r for r in data["runs"]}
    one, two, three = (runs[(n, "main")]["images_per_minute"] for n in (1, 2, 3))
    return f"""## 3. Throughput: a burst of {data['jobs_per_run']} uploads

All {data['jobs_per_run']} photos ({data['photo_size']}) are uploaded at once (16 at a time), then the
workers drain the queue. The machine has 2 vCPUs. "Work per job" is the
median time a worker spends on one job (decode + model + render + store).

{table(["Workers", "Outputs", "Images / min", "Work per job", "Model", "End-to-end p50", "End-to-end p95", "Passed rules"], rows)}

**Findings**

- Two workers on two cores give {two / one:.2f}× the throughput of one ({one:.0f} → {two:.0f} images/min).
- A third worker on the same two cores gives {three:.0f} images/min: no gain, because the work is CPU-bound. Rule: one worker per core, scale out with more machines.
- End-to-end time in a burst is almost all queue wait: work per job is under a second, but the last of {data['jobs_per_run']} jobs waits for all the others. This is why the autoscaling signal should be queue wait, not CPU.
"""


def section_steady(data: dict) -> str:
    jobs = data["jobs"]
    e2e, wait = jobs["end_to_end_ms"], jobs["queue_wait_ms"]
    return f"""## 4. Steady load (normal traffic)

Uploads arrive at a steady {data['arrival_per_minute']:.0f} per minute for {data['duration_s']} s
(about 70% of what {data['workers']} workers can do), main image only.

{table(["Jobs", "End-to-end p50", "p95", "p99", "Queue wait p50", "Queue wait p95", "Upload p50"],
       [[jobs['succeeded'], ms(e2e['p50']), ms(e2e['p95']), ms(e2e['p99']), ms(wait['p50']), ms(wait['p95']),
         ms(data['upload_ms']['p50'])]])}

With spare capacity there is almost no queueing: 99% of sellers get the main
image back within {ms(e2e['p99'])} of uploading.
"""


def section_chaos(data: dict) -> str:
    kills = ", ".join(f"{t:.0f} s" for t in data["kill_times_after_first_upload_s"])
    return f"""## 5. Killing workers in the middle of a run

Two workers, {data['jobs']} jobs uploaded at once, visibility timeout {data['visibility_timeout_ms'] / 1000:.0f} s.
A busy worker is killed with `SIGKILL` (no clean-up, like a machine dying)
{data['workers_killed']} times, at {kills} after the first upload, and a replacement
worker is started each time (what an orchestrator does when a container dies).

{table(["Jobs", "Succeeded", "Failed", "Lost", "Taken over after a kill", "Still pending after the run", "Images / min"],
       [[data['jobs'], data['succeeded'], data['failed'], data['lost_jobs'], data['jobs_taken_over'],
         data['pending_after_run'], f"{data['images_per_minute']:.0f}"]])}

The {data['jobs_taken_over']} jobs that were in progress on the killed workers (a worker takes one
job at a time) stayed in the Redis Streams pending list. After the visibility
timeout another worker claimed each of them with `XAUTOCLAIM` and finished it
on attempt 2. Nothing was lost and nothing was left pending.
"""


def section_overload(data: dict) -> str:
    acc, rej = data["accepted_upload_ms"], data["rejected_upload_ms"]
    jobs = data["accepted_jobs"]
    return f"""## 6. Overload: more uploads than the queue limit

{data['burst']} uploads at once (32 at a time), queue limit {data['max_open_jobs']} open jobs, {data['workers']} worker.

{table(["Accepted (202)", "Rejected (503)", "Other status", "Accepted upload p50", "Rejected upload p50", "Accepted jobs finished"],
       [[data['accepted'], data['rejected_503'], data['other_status'] or "none", ms(acc['p50']), ms(rej['p50']),
         f"{jobs['succeeded']}/{jobs['jobs']}"]])}

Rejected requests get a quick `503` with `Retry-After: 5` instead of waiting in
an ever-growing queue, and every accepted job still finishes.
{before_fix_note()}"""


def before_fix_note() -> str:
    before = load("history/overload_before_atomic_admission")
    if not before:
        return ""
    return f"""
The first version of this benchmark found a bug: with the same settings it
accepted **{before['accepted']} jobs against a limit of {before['max_open_jobs']}**, because the API checked the
counter and incremented it in two separate steps. Now both happen in one Lua
script (see DESIGN.md, 5.11). The old result is kept in
`benchmarks/results/history/`.
"""


def section_api(data: dict) -> str:
    accept = data["accept_ms"]
    return f"""## 7. The API alone

{data['uploads']} uploads (mean photo {data['mean_photo_kb']} KB), 32 at a time, no workers running.

{table(["Accepted", "Uploads / second", "p50", "p95", "p99"],
       [[data['accepted'], data['uploads_per_second'], ms(accept['p50']), ms(accept['p95']), ms(accept['p99'])]])}

One API process (uvicorn, one event loop, sync endpoints in its thread pool)
on the same 2-vCPU machine as the load generator.
"""


def main() -> None:
    env = (load("throughput") or load("model_only"))["environment"]
    parts = [f"""# SnapList benchmarks

Every number below comes from `bench/run_benchmarks.py`, and this file is
written by `bench/make_report.py` from the JSON in
[`benchmarks/results/`](benchmarks/results). Nothing is typed by hand.

**Setup.** A cloud machine with {env['cpus']} vCPUs and {env['ram_gb']} GB RAM, Python {env['python']}.
Each scenario starts real processes: `redis-server`, the API under uvicorn, and
worker processes, each using one thread (`OMP_NUM_THREADS=1`). Photos are
synthetic 1600 × 1200 JPEG product photos made by `tests/photos.py`. The
timings come from the job records that the workers write.

Reproduce: `python bench/run_benchmarks.py all && python bench/make_report.py`
"""]
    sections = [("model_only", section_model), ("render_stages", section_render), ("throughput", section_throughput),
                ("steady_load", section_steady), ("chaos_kill_worker", section_chaos), ("overload", section_overload),
                ("api_accept", section_api)]
    for name, render in sections:
        data = load(name)
        if data:
            parts.append(render(data))
    parts.append("""## Limits of these numbers

- Synthetic photos, one machine, 2 vCPUs. Real phone photos are bigger; they are shrunk to 2048 px first, so the work per job stays close to this.
- The load generator runs on the same machine as the system, so it takes some CPU too.
- Storage is the local disk; with S3 the store step would take longer, but it is not on the critical CPU path.
""")
    (ROOT / "BENCHMARKS.md").write_text("\n".join(parts))
    print((ROOT / "BENCHMARKS.md").read_text())


if __name__ == "__main__":
    main()
