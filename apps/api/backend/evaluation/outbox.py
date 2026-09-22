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
        """有界重试导出(tenacity);永不抛出——观测故障不允许阻塞业务(§9.3)。"""
        from tenacity import Retrying, stop_after_attempt

        events = self.pending()
        if not events:
            return {"flushed": 0, "remaining": 0, "attempts": 0, "lastError": None}
        retrying = Retrying(stop=stop_after_attempt(max_attempts), reraise=True)
        try:
            retrying(self._export_all, exporter, events)
        except Exception as exc:  # 导出失败:保留缓冲,下一窗口重试
            return {
                "flushed": 0,
                "remaining": len(events),
                "attempts": retrying.statistics["attempt_number"],
                "lastError": f"{type(exc).__name__}: {exc}"[:200],
            }
        self.file.write_text("", encoding="utf-8")  # 成功后清空缓冲
        return {
            "flushed": len(events),
            "remaining": 0,
            "attempts": retrying.statistics["attempt_number"],
            "lastError": None,
        }

    @staticmethod
    def _export_all(exporter: Callable[[list[dict]], None], events: list[dict]) -> None:
        exporter(events)
