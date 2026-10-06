"""统一业务错误契约(§八):稳定错误码 + 类型化异常 + 恢复语义。

- HTTP 与 Worker 使用同一分类;恢复动作由结构化字段表达,禁止解析错误文案。
- 文案面向用户;details 仅承载契约允许的安全业务字段(jobId、requestId 等),
  不携带密钥、路径、SQL、供应商原始响应。
- 500 响应使用安全兜底,堆栈只进日志;保留异常原因链,调用方 raise from 串联。
"""

from enum import Enum

import httpx


class ErrorCode(str, Enum):
    INVALID_PARAM = "INVALID_PARAM"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    QUEUE_FULL = "QUEUE_FULL"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    UPSTREAM_AUTH = "UPSTREAM_AUTH"
    UPSTREAM_RATE_LIMIT = "UPSTREAM_RATE_LIMIT"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    UPSTREAM_UNKNOWN = "UPSTREAM_UNKNOWN"
    STORAGE_FAILURE = "STORAGE_FAILURE"
    DATA_CORRUPTED = "DATA_CORRUPTED"
    INTERNAL = "INTERNAL"


class Recovery(str, Enum):
    EDIT_INPUT = "edit_input"  # 修正输入后重试
    RETRY = "retry"  # 稍后原样重试(未产生付费提交)
    RECONCILE = "reconcile"  # 结果未知,先查询上游,绝不重新提交
    SIGN_IN = "sign_in"  # 需要认证
    CONTACT = "contact"  # 联系支持,可复制问题编号
    ABANDON = "abandon"  # 明确放弃,任务不可恢复


class AppError(Exception):
    """业务错误基类;HTTP 与 Worker 共用 code/recovery,堆栈与原因链由日志负责。"""

    code: ErrorCode = ErrorCode.INTERNAL
    http_status: int = 500
    recovery: Recovery = Recovery.CONTACT

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})

    def to_payload(self) -> dict:
        return {
            "error": self.message,
            "code": self.code.value,
            "recovery": self.recovery.value,
            "details": self.details,
        }


class InvalidParamError(AppError):
    code = ErrorCode.INVALID_PARAM
    http_status = 400
    recovery = Recovery.EDIT_INPUT


class ForbiddenError(AppError):
    code = ErrorCode.FORBIDDEN
    http_status = 403
    recovery = Recovery.SIGN_IN


class NotFoundError(AppError):
    code = ErrorCode.NOT_FOUND
    http_status = 404
    recovery = Recovery.EDIT_INPUT


class ConflictError(AppError):
    code = ErrorCode.CONFLICT
    http_status = 409
    recovery = Recovery.EDIT_INPUT


class QueueFullError(AppError):
    code = ErrorCode.QUEUE_FULL
    http_status = 429
    recovery = Recovery.RETRY


class QuotaExhaustedError(AppError):
    code = ErrorCode.QUOTA_EXHAUSTED
    http_status = 429
    recovery = Recovery.RETRY


class UpstreamAuthError(AppError):
    code = ErrorCode.UPSTREAM_AUTH
    http_status = 502
    recovery = Recovery.CONTACT


class UpstreamRateLimitError(AppError):
    code = ErrorCode.UPSTREAM_RATE_LIMIT
    http_status = 429
    recovery = Recovery.RETRY


class UpstreamTimeoutError(AppError):
    code = ErrorCode.UPSTREAM_TIMEOUT
    http_status = 504
    recovery = Recovery.RETRY


class UpstreamUnavailableError(AppError):
    code = ErrorCode.UPSTREAM_UNAVAILABLE
    http_status = 502
    recovery = Recovery.RETRY


class UpstreamUnknownError(AppError):
    code = ErrorCode.UPSTREAM_UNKNOWN
    http_status = 502
    recovery = Recovery.RECONCILE


class StorageError(AppError):
    code = ErrorCode.STORAGE_FAILURE
    http_status = 500
    recovery = Recovery.CONTACT


class DataCorruptedError(AppError):
    code = ErrorCode.DATA_CORRUPTED
    http_status = 500
    recovery = Recovery.CONTACT


# 500 安全兜底:不携带任何异常文本,防止内部细节外泄。
INTERNAL_RESPONSE = {
    "error": "服务器内部错误",
    "code": ErrorCode.INTERNAL.value,
    "recovery": Recovery.CONTACT.value,
    "details": {},
}

# 迁移期兼容:既有 HTTPException 路径按状态映射结构化字段,语义细化随路由迁移逐步替换。
_STATUS_CONTRACT = {
    400: (ErrorCode.INVALID_PARAM, Recovery.EDIT_INPUT),
    401: (ErrorCode.UNAUTHENTICATED, Recovery.SIGN_IN),
    403: (ErrorCode.FORBIDDEN, Recovery.SIGN_IN),
    404: (ErrorCode.NOT_FOUND, Recovery.EDIT_INPUT),
    409: (ErrorCode.CONFLICT, Recovery.EDIT_INPUT),
    422: (ErrorCode.INVALID_PARAM, Recovery.EDIT_INPUT),
    429: (ErrorCode.QUEUE_FULL, Recovery.RETRY),
    502: (ErrorCode.UPSTREAM_UNAVAILABLE, Recovery.RETRY),
    503: (ErrorCode.UPSTREAM_UNAVAILABLE, Recovery.RETRY),
    504: (ErrorCode.UPSTREAM_TIMEOUT, Recovery.RETRY),
}


def status_contract(status: int) -> tuple[ErrorCode, Recovery]:
    return _STATUS_CONTRACT.get(status, (ErrorCode.INTERNAL, Recovery.CONTACT))


def classify_exception(exc: BaseException) -> tuple[ErrorCode, Recovery]:
    """Worker 边界异常分类:仅依据异常类型与结构化属性(如 HTTP 状态码),不解析文案。"""
    if isinstance(exc, AppError):
        return exc.code, exc.recovery
    if isinstance(exc, TimeoutError):
        return ErrorCode.UPSTREAM_TIMEOUT, Recovery.RETRY
    if isinstance(exc, httpx.TimeoutException):
        return ErrorCode.UPSTREAM_TIMEOUT, Recovery.RETRY
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return ErrorCode.UPSTREAM_AUTH, Recovery.CONTACT
        if status == 429:
            return ErrorCode.UPSTREAM_RATE_LIMIT, Recovery.RETRY
        return ErrorCode.UPSTREAM_UNAVAILABLE, Recovery.RETRY
    if isinstance(exc, httpx.TransportError):
        return ErrorCode.UPSTREAM_UNAVAILABLE, Recovery.RETRY
    if isinstance(exc, ValueError):
        return ErrorCode.INVALID_PARAM, Recovery.EDIT_INPUT
    return ErrorCode.INTERNAL, Recovery.CONTACT
