"""Atomic JSON persistence and shared wire-format helpers."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def atomic_write_text(path: Path, text: str) -> None:
    """先写临时文件再原子替换,避免并发/崩溃读取到半写文件(§11.1)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(file: Path, default):
    try:
        return json.loads(file.read_text())
    except (OSError, ValueError):
        return default


def write_json(file: Path, data) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_name(f".{file.name}.{uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, file)
    finally:
        temp.unlink(missing_ok=True)


def valid_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9-]{36}", value))
