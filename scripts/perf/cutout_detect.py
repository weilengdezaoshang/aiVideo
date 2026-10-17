"""TEMP opt-in profiler: isolated HTTP, real production detect handler and rembg.
No product modules are modified. Run with .venv/bin/python scripts/perf/cutout_detect.py.
"""

import ast
import asyncio
import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api"))
OUT = ROOT / "docs/intent"
TMP = Path("/tmp/aivideo-cutout-perf")
FIELDS = [
    "run_id",
    "ts",
    "cold_start",
    "model",
    "sample_name",
    "source_w",
    "source_h",
    "source_bytes",
    "mask_bytes",
    "device_hint",
    "py_version",
    "rembg_version",
    "onnxruntime_version",
    "t_client_total",
    "t_http_read_body",
    "t_queue_wait",
    "t_session_load",
    "t_infer",
    "t_alpha_encode",
    "t_server_total",
    "t_import",
    "t_ort_run",
    "providers",
    "status",
    "note",
]


def child(model, sample, port):
    import uvicorn
    from fastapi import FastAPI, Depends, HTTPException, Request
    from PIL import Image
    from backend.cutout import Recognition, Rembg

    # Keep lazy rembg import inside first inference, and time it separately.
    spans = {}

    class TimedRembg(Rembg):
        def __call__(self, source):
            spans["t_queue_wait"] = (time.perf_counter() - spans["submitted"]) * 1000
            t = time.perf_counter()
            from rembg import new_session, remove

            spans["t_import"] = (time.perf_counter() - t) * 1000
            spans["t_session_load"] = 0.0
            if self.session is None:
                t = time.perf_counter()
                self.session = new_session(self.model)
                spans["t_session_load"] = (time.perf_counter() - t) * 1000
            spans["providers"] = self.session.inner_session.get_providers()
            spans["t_ort_run"] = 0.0
            real = self.session.inner_session

            class OrtProxy:
                def __getattr__(self, name):
                    return getattr(real, name)

                def run(self, *args, **kwargs):
                    t = time.perf_counter()
                    try:
                        return real.run(*args, **kwargs)
                    finally:
                        spans["t_ort_run"] += (time.perf_counter() - t) * 1000

            self.session.inner_session = OrtProxy()
            t = time.perf_counter()
            try:
                output = remove(source, session=self.session, alpha_matting=False)
            finally:
                spans["t_infer"] = (time.perf_counter() - t) * 1000
                self.session.inner_session = real
            t = time.perf_counter()
            with Image.open(io.BytesIO(output)) as image:
                buffer = io.BytesIO()
                image.convert("RGBA").getchannel("A").save(buffer, format="PNG")
                result = buffer.getvalue()
            spans["t_alpha_encode"] = (time.perf_counter() - t) * 1000
            spans["mask_bytes"] = len(result)
            return result

    app = FastAPI()
    app.state.recognition = Recognition(model, TimedRembg(model))
    original_detect = app.state.recognition.detect

    async def timed_detect(ident, source):
        spans["submitted"] = time.perf_counter()
        return await original_detect(ident, source)

    app.state.recognition.detect = timed_detect

    class LocalRedis:
        async def get(self, key):
            return None

    app.state.redis = LocalRedis()

    async def authenticated():
        return SimpleNamespace(workspace_id="perf", principal="perf")

    async def image_bytes(ident, identity):
        t = time.perf_counter()
        try:
            return await asyncio.to_thread(Path(sample).read_bytes)
        finally:
            spans["t_http_read_body"] = (time.perf_counter() - t) * 1000

    def operation_key(operation_id, identity):
        return operation_id

    tree = ast.parse((ROOT / "apps/api/backend/production.py").read_text())
    factory = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "create_app"
    )
    handler = next(
        n for n in factory.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "detect"
    )
    # Execute the exact handler source; substitute only auth, asset lookup and Redis.
    env = dict(
        app=app,
        uuid=uuid,
        Request=Request,
        Identity=SimpleNamespace,
        Depends=Depends,
        authenticated=authenticated,
        operation_key=operation_key,
        image_bytes=image_bytes,
        asyncio=asyncio,
        suppress=suppress,
        RedisError=RuntimeError,
        base64=base64,
        HTTPException=HTTPException,
    )
    handler.decorator_list = []
    exec(
        compile(
            ast.Module(body=[handler], type_ignores=[]),
            "<production detect>",
            "exec",
            dont_inherit=True,
        ),
        env,
    )
    route = env["detect"]

    @app.post("/api/cutout/detect/{ident}")
    async def endpoint(ident: uuid.UUID, request: Request):
        spans.clear()
        t = time.perf_counter()
        try:
            return await route(ident, request, await authenticated())
        finally:
            spans["t_server_total"] = (time.perf_counter() - t) * 1000
            (TMP / f"{port}.json").write_text(json.dumps(spans))

    @app.get("/ready")
    async def ready():
        return {"ready": True}

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


def main():
    import httpx
    import onnxruntime as ort
    from PIL import Image

    TMP.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)
    portrait = ROOT / "data/validation/portrait-cutout-preview.png"
    if not portrait.exists():
        raise SystemExit("Missing local portrait fixture; provide equivalent source explicitly.")
    samples = []
    with Image.open(portrait) as im:
        for name, size in [
            ("S-small", 512),
            ("S-mid", 1024),
            ("S-large", 2048),
            ("S-portrait", 1024),
        ]:
            path = TMP / f"{name}.png"
            im.convert("RGB").resize((size, size), Image.Resampling.LANCZOS).save(path)
            samples.append((name, path, size))
    metadata = dict(
        platform=platform.platform(),
        machine=platform.machine(),
        processor=platform.processor(),
        available_providers=ort.get_available_providers(),
        versions={n: importlib.metadata.version(n) for n in ["rembg", "onnxruntime", "Pillow"]},
        python=sys.version,
        source=str(portrait.relative_to(ROOT)),
        source_sha256=hashlib.sha256(portrait.read_bytes()).hexdigest(),
        samples={name: hashlib.sha256(p.read_bytes()).hexdigest() for name, p, _ in samples},
    )
    (OUT / "perf-cutout-environment.json").write_text(json.dumps(metadata, indent=2))
    with (OUT / "perf-cutout-detect.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        f.flush()
        for model in ["birefnet-portrait", "isnet-general-use"]:
            for name, path, size in samples:
                with socket.socket() as s:
                    s.bind(("127.0.0.1", 0))
                    port = s.getsockname()[1]
                log = (TMP / f"{model}-{name}.log").open("w")
                env = {**os.environ, "CUTOUT_MODEL": model}
                process = subprocess.Popen(
                    [sys.executable, __file__, "--child", model, str(path), str(port)],
                    stdout=log,
                    stderr=log,
                    env=env,
                )
                try:
                    with httpx.Client(
                        base_url=f"http://127.0.0.1:{port}", timeout=150, trust_env=False
                    ) as client:
                        for _ in range(300):
                            if process.poll() is not None:
                                raise RuntimeError(f"child failed: {TMP}/{model}-{name}.log")
                            try:
                                if client.get("/ready").status_code == 200:
                                    break
                            except httpx.ConnectError:
                                pass
                            time.sleep(0.1)
                        else:
                            raise RuntimeError(
                                "isolated API did not become ready within 30 seconds"
                            )
                        for i in range(4):
                            row = dict(
                                run_id=f"{model}-{name}-{i}",
                                ts=datetime.now(timezone.utc).isoformat(),
                                cold_start=i == 0,
                                model=model,
                                sample_name=name,
                                source_w=size,
                                source_h=size,
                                source_bytes=path.stat().st_size,
                                device_hint="unknown",
                                py_version=platform.python_version(),
                                rembg_version=metadata["versions"]["rembg"],
                                onnxruntime_version=metadata["versions"]["onnxruntime"],
                                note="isolated_http; auth/DB/Redis substituted; weights cached",
                            )
                            metrics = TMP / f"{port}.json"
                            metrics.unlink(missing_ok=True)
                            t = time.perf_counter()
                            try:
                                response = client.post(
                                    f"/api/cutout/detect/{uuid.uuid4()}?operationId={uuid.uuid4()}"
                                )
                                payload = response.json()
                                if response.status_code == 200:
                                    mask = base64.b64decode(
                                        payload["urls"]["original"].split(",", 1)[1]
                                    )
                                    with Image.open(io.BytesIO(mask)) as mask_image:
                                        assert mask_image.size == (size, size)
                                row["status"] = response.status_code
                                if response.status_code != 200:
                                    row["note"] += "; " + str(payload)
                            except Exception as exc:
                                row["note"] += "; " + repr(exc)
                                row["status"] = "error"
                            row["t_client_total"] = (time.perf_counter() - t) * 1000
                            metrics = TMP / f"{port}.json"
                            if metrics.exists():
                                measured = json.loads(metrics.read_text())
                                row.update({k: v for k, v in measured.items() if k in FIELDS})
                                row["providers"] = json.dumps(row.get("providers", []))
                                row["device_hint"] = (
                                    "cpu"
                                    if "CPUExecutionProvider" in row["providers"]
                                    else "unknown"
                                )
                            writer.writerow(row)
                            f.flush()
                            print(
                                row["run_id"],
                                row["status"],
                                round(row["t_client_total"], 1),
                                "ms",
                                flush=True,
                            )
                            if row["status"] != 200:
                                break
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    log.close()


if __name__ == "__main__":
    if "--child" in sys.argv:
        child(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    else:
        main()
