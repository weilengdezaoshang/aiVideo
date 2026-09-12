"""入口级步骤追踪(技术方案 §9,验收 TRACE-01/02/03)。

- trace 从 API 入口建立,覆盖尚未创建 job 的路径(参数校验、翻译失败降级)。
- 步骤名固定低基数(§9.1);原始提示词不作为标签,只存哈希与长度。
- 重启恢复建新 trace,通过 traceLinks 关联原 trace,不伪造同一持续 span。
- 本地写入永不因第三方观测故障被阻塞;导出走 outbox(P3)。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from ..common import now

TRACE_SCHEMA_VERSION = 1


def prompt_fingerprint(prompt: str) -> dict:
    """提示词的脱敏指纹(§9.2):不落原文,只落哈希与长度。"""
    return {
        "promptHash": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12] if prompt else None,
        "promptChars": len(prompt or ""),
    }


class TraceRecorder:
    """JSONL 追加式步骤追踪;单进程单写者,与既有 traces.jsonl(终态日志)互补。"""

    def __init__(self, file: Path):
        self.file = file
        self._lock = threading.Lock()
        self._sequence = 0
        if file.is_file():
            with file.open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        self._sequence += 1

    def append(self, record: dict) -> dict:
        self._sequence += 1
        entry = {"schemaVersion": TRACE_SCHEMA_VERSION, "sequence": self._sequence, "ts": now(), **record}
        with self._lock:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with self.file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def trace_started(self, trace_id, origin="production", **attrs) -> dict:
        return self.append({"kind": "trace-started", "traceId": trace_id, "origin": origin, **attrs})

    def step(self, trace_id, name, status="finished", jobId=None, **attrs) -> dict:
        return self.append(
            {"kind": "step", "traceId": trace_id, "step": name, "status": status, "jobId": jobId, **attrs}
        )

    def recovery(self, new_trace_id, previous_trace_id, jobId, reason) -> dict:
        return self.append(
            {
                "kind": "trace-recovery",
                "traceId": new_trace_id,
                "traceLinks": [previous_trace_id],
                "jobId": jobId,
                "reason": reason,
            }
        )

    def read_all(self, trace_id=None) -> list[dict]:
        if not self.file.is_file():
            return []
        records = []
        for line in self.file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if trace_id is None or item.get("traceId") == trace_id:
                records.append(item)
        return records


class NoopTracer(TraceRecorder):
    """生产默认关闭详细步骤时的零开销占位;不写任何文件。"""

    def __init__(self):
        super().__init__(Path("/dev/null"))

    def append(self, record: dict) -> dict:  # type: ignore[override]
        return {}


def start_trace_id(prefix: str = "trace") -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:16]}"
