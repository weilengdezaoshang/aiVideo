"""Isolated legacy backend baseline. Run with .venv/bin/python; no provider credentials."""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api"))


def summary(values):
    ordered = sorted(values)
    return {
        "samples": len(values),
        "p50_ms": round(statistics.median(values), 2),
        "p95_ms": round(ordered[math.ceil(0.95 * len(values)) - 1], 2),
        "max_ms": round(max(values), 2),
        "samples_ms": [round(value, 3) for value in values],
    }


def fixture(data, count):
    folder = data / "documents"
    folder.mkdir(parents=True)
    for index in range(count):
        ident = str(uuid.UUID(int=index + 1))
        doc = {
            "id": ident,
            "name": "baseline",
            "revision": 1,
            "updatedAt": "2026-10-03T00:00:00Z",
            "order": ["text"],
            "objects": {"text": {"id": "text", "kind": "text", "text": "x" * 100_000}},
        }
        (folder / (ident + ".json")).write_text(json.dumps(doc))
    return sum(path.stat().st_size for path in folder.glob("*.json"))


async def http_baseline(data):
    import httpx

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SWARMUI_", "AIVERO_", "CUTOUT_"))
    }
    config = data.parent / "config.json"
    config.write_text(json.dumps({"provider": "mock", "port": port}))
    env.update(SWARMUI_CONFIG=str(config), SWARMUI_DATA_DIR=str(data), SWARMUI_ENV="development")
    process = subprocess.Popen(
        [sys.executable, __file__, "--serve", str(data), "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=60, trust_env=False
        ) as client:
            for _ in range(200):
                try:
                    (await client.get("/api/health")).raise_for_status()
                    break
                except httpx.TransportError:
                    if process.poll() is not None:
                        raise RuntimeError("isolated server exited")
                    await asyncio.sleep(0.05)
            else:
                raise RuntimeError("isolated server startup timed out")

            async def request(path):
                started = time.perf_counter()
                response = await client.get(path)
                response.raise_for_status()
                return (time.perf_counter() - started) * 1000

            idle = [await request("/api/health") for _ in range(20)]
            documents = [await request("/api/documents") for _ in range(20)]
            # Each round queues document work followed by unrelated health work.
            mixed_health, mixed_documents = [], []
            started = time.perf_counter()
            for _ in range(5):
                results = await asyncio.gather(
                    *(request(path) for path in ["/api/documents", "/api/health"] * 4)
                )
                mixed_documents.extend(results[::2])
                mixed_health.extend(results[1::2])
            elapsed = time.perf_counter() - started
            generation_health = []
            response = await client.post(
                "/api/generate",
                json={
                    "prompt": "baseline",
                    "model": "mock-diffusion-xl",
                    "width": 64,
                    "height": 64,
                    "seed": 1,
                },
            )
            response.raise_for_status()
            job_id = response.json()["jobId"]
            started = time.perf_counter()
            while time.perf_counter() - started < 60:
                generation_health.append(await request("/api/health"))
                job = (await client.get("/api/jobs/" + job_id)).json()["job"]
                if job["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            if job["status"] != "completed":
                raise RuntimeError("Mock generation did not complete")
            return {
                "health_idle": summary(idle),
                "documents_serial": summary(documents),
                "mixed_concurrency": 8,
                "mixed_health": summary(mixed_health),
                "mixed_documents": summary(mixed_documents),
                "mixed_requests_per_s": round(40 / elapsed, 2),
                "health_during_mock_generation": summary(generation_health),
                "mock_completion_ms": round((time.perf_counter() - started) * 1000, 2),
            }
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve")
    parser.add_argument("--port", type=int)
    parser.add_argument("--output", default="docs/performance/backend-baseline-2026-10-04.json")
    args = parser.parse_args()
    if args.serve:
        import uvicorn
        from backend.evaluation.sandbox import create_sandbox_app as create_app
        from backend.config import Config

        uvicorn.run(
            create_app(root=ROOT, data_dir=Path(args.serve), config=Config(provider="mock")),
            host="127.0.0.1",
            port=args.port,
            log_level="error",
            access_log=False,
        )
        return
    from backend.storage import Documents, History
    from backend.common import write_json

    result = {
        "mode": "legacy-file-api",
        "provider": "mock",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "working_tree_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
        ),
        "fixture": "100000 ASCII characters per document, warm filesystem cache",
        "scenarios": [],
    }
    for count in (10, 100, 1000):
        with tempfile.TemporaryDirectory(prefix="aivideo-backend-baseline-") as temporary:
            data = Path(temporary) / "data"
            size = fixture(data, count)
            documents, history = Documents(data / "documents"), History(data)
            documents.list()
            values = []
            for _ in range(20):
                start = time.perf_counter()
                documents.list()
                values.append((time.perf_counter() - start) * 1000)
            saves = []
            for _ in range(20):
                start = time.perf_counter()
                history.save("baseline", "mock", {"prompt": "baseline"}, b"bench", "png")
                saves.append((time.perf_counter() - start) * 1000)
            scenario = {
                "document_count": count,
                "document_bytes": size,
                "documents_list": summary(values),
                "history_save": summary(saves),
                "http": asyncio.run(http_baseline(data)),
            }
            result["scenarios"].append(scenario)
            print(json.dumps(scenario), flush=True)
    result["json_rewrite"] = []
    with tempfile.TemporaryDirectory(prefix="aivideo-json-baseline-") as temporary:
        for count in (100, 1000, 10000):
            payload = {
                "jobs": [
                    {"id": str(index), "status": "completed", "params": {"prompt": "x" * 200}}
                    for index in range(count)
                ]
            }
            path = Path(temporary) / "jobs.json"
            values = []
            for _ in range(20):
                start = time.perf_counter()
                write_json(path, payload)
                values.append((time.perf_counter() - start) * 1000)
            result["json_rewrite"].append(
                {"jobs": count, "bytes": path.stat().st_size, **summary(values)}
            )
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print("Saved " + str(output))


if __name__ == "__main__":
    main()
