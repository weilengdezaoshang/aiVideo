"""裁判评分缓存(技术方案 §13.4,验收 CACHE-01/02/03)。

缓存键覆盖(§13.4):素材哈希、检查项列表(含依赖与顺序)、rubric 版本、裁判适配器
与模型修订、裁判参数(温度/最大尝试/提示词版本)、预处理版本。任一变化 → 新键 →
缓存失效(CACHE-02)。
条目保存首次计算的 usage 与费用口径引用:并发命中方不重复计费(CACHE-01),
但保留原始评分、usage 与 cacheSource,报告能区分"本次调用"与"缓存复用"。
错误结果默认不入缓存(裁判瞬时报错不应被固化);显式传入 cache_errors 才保存。
缓存不是录制包,也不能替代 live 稳定性试验(CACHE-03):稳定性试验显式绕过缓存。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ..common import now
from .models import canonical_hash
from .recorder import body_hash


def grade_cache_key(
    artifact_sha256: str,
    checks_dump: list[dict],
    rubric_version: str,
    judge_adapter: str,
    judge_model: str | None,
    preprocessing_version: str = "1",
    locale: str = "zh-CN",
    judge_params: dict | None = None,
) -> str:
    return body_hash(
        json.dumps(
            {
                "artifact": artifact_sha256,
                "checks": canonical_hash(checks_dump),
                "rubric": rubric_version,
                "judgeAdapter": judge_adapter,
                "judgeModel": judge_model,
                "preprocessing": preprocessing_version,
                "locale": locale,
                "judgeParams": canonical_hash(judge_params or {}),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    )


class GradeCache:
    """JSONL 追加式缓存;single-flight 用进程锁 + in-flight 注册表实现。"""

    def __init__(self, store: Path):
        self.file = store / "grade-cache" / "entries.jsonl"
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._results: dict[str, dict] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded or not self.file.is_file():
            self._loaded = True
            return
        for line in self.file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                # 同键多行:最后一条胜出(append-only 日志的读取语义)。
                self._results[item["key"]] = item

    def lookup(self, key: str) -> dict | None:
        with self._lock:
            self._load()
            return self._results.get(key)

    def store(
        self,
        key: str,
        answers: dict[str, dict],
        judge_error: str | None,
        usage: dict | None = None,
        cost_basis: str | None = None,
        cache_errors: bool = False,
    ) -> None:
        if judge_error and not cache_errors:
            return  # 错误结果不固化:下次重新判分,瞬时报错不进入缓存
        with self._lock:
            entry = {
                "key": key,
                "answers": answers,
                "judgeError": judge_error,
                "usage": usage,
                "costBasis": cost_basis,
                "cachedAt": now(),
            }
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with self.file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._results[key] = entry

    def singleflight(self, key: str, compute=None):
        """并发相同键:只有一个调用真正 compute,其余等待后读共享结果。

        返回 (entry, cacheHit):entry 是缓存条目(answers/judgeError/usage/cachedAt);
        compute 签名: () -> entry dict。
        """
        cached = self.lookup(key)
        if cached is not None:
            return cached, True
        with self._lock:
            self._load()
            cached = self._results.get(key)
            if cached is not None:
                return cached, True
            event = self._inflight.get(key)
            if event is None:
                event = threading.Event()
                self._inflight[key] = event
                owner = True
            else:
                owner = False
        if owner:
            try:
                entry = compute() if compute else {"answers": {}, "judgeError": None}
                self.store(
                    key,
                    entry.get("answers") or {},
                    entry.get("judgeError"),
                    usage=entry.get("usage"),
                    cost_basis=entry.get("costBasis"),
                )
                return entry, False
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                event.set()
        else:
            event.wait(30)
            cached = self.lookup(key)
            if cached is None:
                # 拥有者异常退出未写结果:自行计算,不冒充共享结果
                entry = compute() if compute else {"answers": {}, "judgeError": None}
                return entry, False
            return cached, True
