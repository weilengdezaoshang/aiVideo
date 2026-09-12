"""观测导出 outbox(技术方案 §9.3/§14.3,验收 TRACE-03)。

本地关键记录先落盘;第三方导出异步批处理、有界重试、失败不阻塞也不丢失缓冲。
Langfuse/Promptfoo 是适配展示入口(§3.3):这里只输出事件副本,不做唯一事实源。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from ..common import now


class Outbox:
    def __init__(self, file: Path):
        self.file = file
        self._lock = threading.Lock()

    def push(self, event: dict) -> None:
        with self._lock:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with self.file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"pushedAt": now(), **event}, ensure_ascii=False) + "\n")

    def pending(self) -> list[dict]:
        if not self.file.is_file():
            return []
        return [
            json.loads(line)
            for line in self.file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def flush(
        self, exporter: Callable[[list[dict]], None], max_attempts: int = 3
    ) -> dict:
        """有界重试导出;永不抛出——观测故障不允许阻塞业务(§9.3)。"""
        events = self.pending()
        if not events:
            return {"flushed": 0, "remaining": 0, "attempts": 0, "lastError": None}
        last_error = None
        attempts = 0
        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            try:
                exporter(events)
                last_error = None
                break
            except Exception as exc:  # 导出失败:保留缓冲,下一窗口重试
                last_error = f"{type(exc).__name__}: {exc}"[:200]
        if last_error is not None:
            return {"flushed": 0, "remaining": len(events), "attempts": attempts, "lastError": last_error}
        self.file.write_text("", encoding="utf-8")  # 成功后清空缓冲
        return {"flushed": len(events), "remaining": 0, "attempts": attempts, "lastError": None}
