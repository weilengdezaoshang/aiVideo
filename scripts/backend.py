"""Portable npm bridge: run the project virtualenv without activating a shell."""

import os
import signal
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
if not python.exists():
    sys.exit(
        "请先使用 Python 3.11+ 执行 python3 -m venv .venv，再执行 .venv/bin/python -m pip install -r requirements-dev.txt"
    )
os.environ["PYTHONPATH"] = str(root / "apps/api") + os.pathsep + str(root)
args = sys.argv[1:]
if args and args[0] == "verify":
    for command in [
        [
            str(python),
            "-m",
            "ruff",
            "check",
            "apps/api/backend",
            "tests/backend",
            "scripts/backend.py",
            "scripts/eval.py",
        ],
        [str(python), "-m", "pytest", "-q"],
    ]:
        subprocess.run(command, cwd=root, check=True)
else:
    compiler = root / "node_modules/typescript/bin/tsc"
    react_builder = ["node", "--import", "tsx", "scripts/build-react.ts"]
    subprocess.run(["node", str(compiler), "-p", "tsconfig.web.json"], cwd=root, check=True)
    subprocess.run(react_builder, cwd=root, check=True)
    watchers = []
    if "--reload" in args:
        watchers.append(
            subprocess.Popen(
                [
                    "node",
                    str(compiler),
                    "-p",
                    "tsconfig.web.json",
                    "--watch",
                    "--preserveWatchOutput",
                ],
                cwd=root,
            )
        )
        watchers.append(
            subprocess.Popen(
                [*react_builder, "--watch"],
                cwd=root,
            )
        )
    child = subprocess.Popen([str(python), "-m", "backend", *args], cwd=root)

    def stop(signum, _frame):
        child.send_signal(signum)
        for watcher in watchers:
            watcher.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        sys.exit(child.wait())
    finally:
        for watcher in watchers:
            watcher.terminate()
            watcher.wait()
