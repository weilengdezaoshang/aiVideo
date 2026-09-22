"""结构化日志与请求上下文(§九)。

- 请求 ID 用 ContextVar 承载,跨中间件/路由/任务传递,并发请求互不串号;
  与业务幂等 ID(clientRef/requestId 参数)相互独立。
- 日志脱敏只依据结构化键名与键值对模式,不承载业务语义判断。
- 多进程部署输出到 stdout,由部署层统一收集;JSON 格式通过 SWARMUI_LOG_FORMAT=json 开启。
"""

import contextvars
import json
import logging
import re
import sys

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

# 脱敏:键名命中即整体遮蔽;键值对文本(如 apiKey=xxx、Authorization: Bearer xxx)值遮蔽。
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|token|password|secret|signature)", re.IGNORECASE
)
_SENSITIVE_PAIR_RE = re.compile(
    r"(api[_-]?key|authorization|token|password|secret)(\s*[=:]\s*)(\S+)", re.IGNORECASE
)
_REDACTED = "[REDACTED]"


def set_request_id(value: str) -> None:
    request_id_var.set(value)


def get_request_id() -> str:
    return request_id_var.get()


def redact_text(text: str) -> str:
    text = re.sub(r"(?i)(authorization\s*[=:]\s*)(?:bearer|basic)\s+[^\s,;]+",
                  lambda match: match.group(1) + _REDACTED, text)
    text = re.sub(r"(https?|postgresql(?:\+psycopg)?|amqps?|redis)://[^\s'\"]+",
                  lambda match: match.group(1) + "://[REDACTED]", text)
    return _SENSITIVE_PAIR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)


def redact_mapping(mapping: dict) -> dict:
    return {k: (_REDACTED if _SENSITIVE_KEY_RE.search(str(k)) else v) for k, v in mapping.items()}


class RequestContextFilter(logging.Filter):
    """为每条日志注入当前请求 ID(无请求上下文时为空串)。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.requestId = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """结构化 stdout 行;堆栈进日志,消息文本先脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
            # 过滤器已附着 requestId 时直接使用;直连 formatter 的场景回退读上下文。
            "requestId": getattr(record, "requestId", request_id_var.get()),
        }
        if record.exc_info:
            payload["stack"] = redact_text(self.formatException(record.exc_info))
        for key in ("jobId", "executionEpoch", "phase", "elapsedMs", "method", "path", "status", "durationMs"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """进程级日志初始化:stdout 单 handler,避免重复记录;JSON 由部署层按需开启。"""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestContextFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(requestId)s] %(name)s: %(message)s",
            )
        )
    root.addHandler(handler)
    root.setLevel(level.upper())


def access_logger() -> logging.Logger:
    return logging.getLogger("frayune.access")


def log_access(method: str, path: str, status: int, duration_ms: int, request_id: str) -> None:
    """访问日志:结构化字段附着在 record 上,便于 JSON/文本两种格式统一输出。"""
    access_logger().info(
        "%s %s -> %s %sms",
        method,
        path,
        status,
        duration_ms,
        extra={
            "requestId": request_id,
            "method": method,
            "path": path,
            "status": status,
            "durationMs": duration_ms,
        },
    )
