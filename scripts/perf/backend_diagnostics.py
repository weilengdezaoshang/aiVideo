"""CPU/function/trace diagnostics on synthetic legacy fixtures; no paid providers."""

import argparse
import asyncio
from collections import defaultdict
from contextvars import ContextVar
import cProfile
from functools import wraps
import html
import inspect
import json
from pathlib import Path
import platform
import pstats
import sys
import tempfile
import time

from backend_baseline import ROOT, fixture, summary

sys.path.insert(0, str(ROOT / "apps/api"))


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


class CPUStacks:
    """Deterministic Python/C instrumentation using current-thread CPU, not wall time."""

    def __init__(self):
        self.stack = []
        self.weights = defaultdict(int)
        self.previous = 0

    def callback(self, frame, event, arg):
        current = time.thread_time_ns()
        if self.stack:
            self.weights[tuple(self.stack)] += max(0, current - self.previous)
        if event == "call":
            self.stack.append(
                f"{frame.f_code.co_filename}:{frame.f_code.co_firstlineno}:{frame.f_code.co_name}"
            )
        elif event == "c_call":
            self.stack.append("native:" + getattr(arg, "__qualname__", str(type(arg))))
        elif event in {"return", "c_return", "c_exception"} and self.stack:
            self.stack.pop()
        self.previous = time.thread_time_ns()

    def export(self, output, stem):
        names = sorted({name for stack in self.weights for name in stack})
        indices = {name: index for index, name in enumerate(names)}
        total = sum(self.weights.values()) / 1e9
        save(
            output / (stem + ".speedscope.json"),
            {
                "$schema": "https://www.speedscope.app/file-format-schema.json",
                "shared": {"frames": [{"name": name} for name in names]},
                "profiles": [
                    {
                        "type": "sampled",
                        "name": stem + " instrumented thread CPU",
                        "unit": "seconds",
                        "startValue": 0,
                        "endValue": total,
                        "samples": [[indices[name] for name in stack] for stack in self.weights],
                        "weights": [weight / 1e9 for weight in self.weights.values()],
                    }
                ],
                "activeProfileIndex": 0,
                "exporter": "aivideo deterministic profile events",
            },
        )
        tree = {"weight": 0, "children": {}}
        for stack, weight in self.weights.items():
            node = tree
            node["weight"] += weight
            for name in stack:
                node = node["children"].setdefault(name, {"weight": 0, "children": {}})
                node["weight"] += weight
        depth = max((len(stack) for stack in self.weights), default=1)
        height = 80 + depth * 22
        pieces = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="{height}">',
            '<rect width="100%" height="100%" fill="#fafafa"/>',
            f'<text x="12" y="24" font-family="sans-serif">{stem}: instrumented Python/C thread CPU, {total:.3f}s</text>',
            '<text x="12" y="46" font-family="sans-serif" font-size="12">Width = CPU attribution; hover for function. Profiler overhead affects absolute times.</text>',
        ]

        def draw(node, x, width, level):
            offset = x
            for name, child in sorted(node["children"].items()):
                span = width * child["weight"] / node["weight"]
                if span >= 0.3:
                    y = height - 24 - level * 22
                    color = f"hsl({sum(name.encode()) % 55},80%,68%)"
                    label = name.rsplit("/", 1)[-1]
                    title = html.escape(f"{name}: {child['weight'] / 1e6:.3f} ms inclusive CPU")
                    pieces.append(
                        f'<g><title>{title}</title><rect x="{offset:.2f}" y="{y}" width="{span:.2f}" height="20" fill="{color}" stroke="white"/>'
                    )
                    if span > 45:
                        pieces.append(
                            f'<text x="{offset + 3:.2f}" y="{y + 14}" font-size="11" font-family="monospace">{html.escape(label[: int(span / 7)])}</text>'
                        )
                    pieces.append("</g>")
                    draw(child, offset, span, level + 1)
                offset += span

        draw(tree, 0, 1400, 0)
        pieces.append("</svg>")
        (output / (stem + ".flame.svg")).write_text("".join(pieces))
        return {
            "attributed_thread_cpu_ms": round(total * 1000, 2),
            "distinct_stacks": len(self.weights),
        }


def cpu_functions(output, count):
    from backend.storage import Documents, History

    with tempfile.TemporaryDirectory(prefix="aivideo-profile-") as temp:
        data = Path(temp) / "data"
        size = fixture(data, count)
        documents, history = Documents(data / "documents"), History(data)

        def work():
            for _ in range(3):
                documents.list()
                history.save("baseline", "mock", {"prompt": "synthetic"}, b"bench", "png")

        work()  # warm cache, no profiler
        profile = cProfile.Profile(timer=time.thread_time)
        start_wall, start_cpu = time.perf_counter(), time.thread_time()
        profile.runcall(work)
        profile.dump_stats(str(output / f"functions-{count}.pstats"))
        totals = {
            "wall_ms": (time.perf_counter() - start_wall) * 1000,
            "thread_cpu_ms": (time.thread_time() - start_cpu) * 1000,
        }
        stats = pstats.Stats(profile)
        functions = [
            {
                "file": key[0],
                "line": key[1],
                "function": key[2],
                "primitive_calls": values[0],
                "calls": values[1],
                "self_cpu_ms": round(values[2] * 1000, 3),
                "inclusive_cpu_ms": round(values[3] * 1000, 3),
            }
            for key, values in stats.stats.items()
        ]
        functions.sort(key=lambda row: row["self_cpu_ms"], reverse=True)
        save(
            output / f"functions-{count}.json",
            {"timer": "thread CPU", **totals, "functions": functions},
        )
        stacks = CPUStacks()
        try:
            stacks.previous = time.thread_time_ns()
            sys.setprofile(stacks.callback)
            work()
        finally:
            sys.setprofile(None)
        return {
            "documents": count,
            "document_bytes": size,
            "cprofile": totals,
            "flame": stacks.export(output, f"cpu-{count}"),
            "top_self_cpu": functions[:10],
        }


async def chain(output, count):
    import httpx
    from backend.evaluation.sandbox import create_sandbox_app as create_app
    from backend.config import Config
    from backend.evaluation.sandbox_jobs import Jobs
    from backend.providers.mock import MockProvider
    from backend.storage import History
    import backend.storage as storage
    import backend.evaluation.sandbox_jobs as jobs_module

    operation = ContextVar("baseline_operation", default="")
    events, records, patches = [], [], []
    epoch = time.perf_counter_ns()
    sequence = 0

    def wrap(owner, attribute, name, bind_job=False):
        original = getattr(owner, attribute)

        def begin(args):
            nonlocal sequence
            token = operation.set(str(args[1])) if bind_job else None
            sequence += 1
            return sequence, time.perf_counter_ns(), operation.get(), token

        def end(info):
            ident, start, job, token = info
            finish = time.perf_counter_ns()
            record = {
                "name": name,
                "span_id": ident,
                "job_id": job,
                "start_ms": (start - epoch) / 1e6,
                "duration_ms": (finish - start) / 1e6,
            }
            records.append(record)
            events.append(
                {
                    "name": name,
                    "cat": "backend",
                    "ph": "X",
                    "ts": (start - epoch) / 1000,
                    "dur": (finish - start) / 1000,
                    "pid": 1,
                    "tid": job or "api",
                    "args": {"span_id": ident, "job_id": job},
                }
            )
            if token is not None:
                operation.reset(token)

        if inspect.iscoroutinefunction(original):

            @wraps(original)
            async def wrapped(*args, **kwargs):
                info = begin(args)
                try:
                    return await original(*args, **kwargs)
                finally:
                    end(info)
        else:

            @wraps(original)
            def wrapped(*args, **kwargs):
                info = begin(args)
                try:
                    return original(*args, **kwargs)
                finally:
                    end(info)

        patches.append((owner, attribute, original))
        setattr(owner, attribute, wrapped)

    wrap(Jobs, "run", "job.execute", True)
    wrap(MockProvider, "generate", "provider.generate")
    wrap(History, "save", "artifact.persist")
    wrap(storage, "referenced_history_files", "history.reference_scan")
    wrap(storage, "write_json", "storage.json_fsync")
    wrap(jobs_module, "write_json", "jobs.json_fsync")
    try:
        with tempfile.TemporaryDirectory(prefix="aivideo-chain-") as temp:
            data = Path(temp) / "data"
            fixture(data, count)
            app = create_app(root=ROOT, data_dir=data, config=Config(provider="mock"))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://baseline",
                    trust_env=False,
                ) as client:

                    async def submit():
                        start = time.perf_counter_ns()
                        response = await client.post(
                            "/api/generate",
                            json={
                                "prompt": "synthetic",
                                "model": "mock-diffusion-xl",
                                "width": 64,
                                "height": 64,
                                "seed": 1,
                            },
                        )
                        response.raise_for_status()
                        ident = response.json()["jobId"]
                        events.append(
                            {
                                "name": "http.accept",
                                "ph": "X",
                                "ts": (start - epoch) / 1000,
                                "dur": (time.perf_counter_ns() - start) / 1000,
                                "pid": 1,
                                "tid": ident,
                            }
                        )
                        return ident, start

                    requests = await asyncio.gather(*(submit() for _ in range(8)))
                    deadline = time.monotonic() + 120
                    while any(
                        app.state.jobs.jobs[ident]["status"] not in {"completed", "failed"}
                        for ident, _ in requests
                    ):
                        if time.monotonic() > deadline:
                            raise RuntimeError("chain timed out")
                        await asyncio.sleep(0.01)
                    results = []
                    from backend.traces import timestamp

                    for ident, request_start in requests:
                        job = app.state.jobs.jobs[ident]
                        if job["status"] != "completed":
                            raise RuntimeError("synthetic job failed")
                        spans = [record for record in records if record["job_id"] == ident]
                        execution = next(span for span in spans if span["name"] == "job.execute")
                        request_end = execution["start_ms"] + execution["duration_ms"]
                        queue_ms = (
                            timestamp(job["startedAt"]) - timestamp(job["createdAt"])
                        ).total_seconds() * 1000
                        events.append(
                            {
                                "name": "queue.wait",
                                "ph": "X",
                                "ts": execution["start_ms"] * 1000 - queue_ms * 1000,
                                "dur": queue_ms * 1000,
                                "pid": 1,
                                "tid": ident,
                            }
                        )
                        results.append(
                            {
                                "job_id": ident,
                                "trace_id": job.get("traceId"),
                                "queue_ms": queue_ms,
                                "request_to_finished_ms": request_end
                                - (request_start - epoch) / 1e6,
                                "provider_ms": sum(
                                    span["duration_ms"]
                                    for span in spans
                                    if span["name"] == "provider.generate"
                                ),
                                "persist_ms": sum(
                                    span["duration_ms"]
                                    for span in spans
                                    if span["name"] == "artifact.persist"
                                ),
                            }
                        )
                    save(
                        output / f"chain-{count}.json",
                        {
                            "mode": "legacy ASGI in-process, 8 concurrent Mock jobs, capacity 2",
                            "jobs": results,
                            "spans": records,
                            "summary": {
                                key: summary([row[key] for row in results])
                                for key in (
                                    "queue_ms",
                                    "request_to_finished_ms",
                                    "provider_ms",
                                    "persist_ms",
                                )
                            },
                        },
                    )
                    save(
                        output / f"chain-{count}.trace.json",
                        {"traceEvents": events, "displayTimeUnit": "ms"},
                    )
                    for name in ("trace-steps.jsonl", "traces.jsonl"):
                        (output / f"chain-{count}-{name}").write_bytes((data / name).read_bytes())
                    return {
                        key: summary([row[key] for row in results])
                        for key in ("queue_ms", "request_to_finished_ms", "persist_ms")
                    }
    finally:
        for owner, attribute, original in reversed(patches):
            setattr(owner, attribute, original)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/performance/backend-diagnostics-2026-10-04")
    args = parser.parse_args()
    output = ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)
    result = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "profiling": "deterministic instrumentation; Python/C boundary, not native-stack sampling",
        "cpu": [],
        "chains": [],
    }
    for count in (100, 1000):
        result["cpu"].append(cpu_functions(output, count))
        print("CPU collected:", count, flush=True)
        result["chains"].append({"documents": count, **asyncio.run(chain(output, count))})
        print("Chain collected:", count, flush=True)
    save(output / "summary.json", result)


if __name__ == "__main__":
    main()
