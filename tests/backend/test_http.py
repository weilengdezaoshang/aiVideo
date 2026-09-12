"""Real TCP regression: existing Node smoke client plus SSE live job delivery."""

import json
import os
import socket
import subprocess
import sys
import time

import httpx

from backend.app import ROOT
from backend.config import ENV


def test_live_http_and_sse(root):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen()
    env = {k: v for k, v in os.environ.items() if k not in ENV}
    # 子进程不走 pytest 的 pythonpath ini,需像生产启动器 scripts/backend.py 一样显式设置。
    env["PYTHONPATH"] = str(ROOT / "apps/api") + os.pathsep + str(ROOT)
    env["TEST_APP_ROOT"] = str(root)
    env["TEST_LISTENER_FD"] = str(listener.fileno())
    code = (
        "import os,uvicorn; from pathlib import Path; from backend.app import create_app; "
        'uvicorn.run(create_app(Path(os.environ["TEST_APP_ROOT"])), '
        'fd=int(os.environ["TEST_LISTENER_FD"]), log_level="error")'
    )
    with (root / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            pass_fds=(listener.fileno(),),
            stdout=log,
            stderr=log,
        )
        listener.close()
        base = f"http://127.0.0.1:{port}"
        try:
            with httpx.Client(base_url=base, timeout=5) as client:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        if client.get("/api/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert process.poll() is None, (root / "server.log").read_text()
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                result = subprocess.run(
                    ["node", "scripts/e2e-smoke.mjs"],
                    cwd=ROOT,
                    env={**env, "SMOKE_BASE_URL": base},
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert result.returncode == 0, result.stdout + result.stderr
                assert "通过 14 / 14" in result.stdout
                with client.stream("GET", "/api/events") as stream:
                    lines = stream.iter_lines()
                    while next(lines) != "event: snapshot":
                        pass
                    snapshot = json.loads(next(lines).removeprefix("data: "))
                    assert snapshot["provider"] == "mock"
                    response = client.post(
                        "/api/generate",
                        json={
                            "prompt": "SSE",
                            "model": "mock",
                            "clientRef": "sse-node",
                            "width": 64,
                            "height": 64,
                        },
                    )
                    ident = response.json()["jobId"]
                    completed, image_seen = False, False
                    event = ""
                    for line in lines:
                        if line.startswith("event: "):
                            event = line[7:]
                        if line.startswith("data: "):
                            payload = json.loads(line[6:])
                            if event == "image" and payload["jobId"] == ident:
                                image_seen = True
                            if (
                                event == "job"
                                and payload["id"] == ident
                                and payload["status"] == "completed"
                            ):
                                completed = True
                                assert payload["clientRef"] == "sse-node"
                                break
                    assert completed and image_seen
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        # Graceful shutdown must flush the last completed job.
        assert json.loads((root / "data/jobs.json").read_text())["jobs"]
