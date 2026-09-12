"""注入式 HTTP 录制(技术方案 §8):在 httpx transport 边界记录每次外部调用。

- 录制不改业务语义:app → jobs → provider 代码原样执行,仅 transport 被替换。
- 请求/响应头不参与匹配;Authorization 等敏感头在写入前脱敏(§16)。
- 媒体/大体积字节进 objects 内容寻址库,body 只在文本且小于阈值时内联。
- sealed 状态表示录制完整可回放;partial 不能自动升级(§8.6)。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx

from ..common import atomic_write_text, now
from .artifacts import ObjectStore, sha256_bytes

MAX_INLINE_BODY = 256 * 1024
REDACT_HEADERS = {"authorization", "x-api-key", "api-key", "cookie", "x-dashscope-async"}
KEEP_RESPONSE_HEADERS = ["content-type"]
# 请求匹配协议版本(§8.3):v2 = method+host+port+path+排序 query+body 规范化哈希,
# 并按 trialId/callSite 分桶。动态字段忽略必须显式配置在 replay 选项里,默认全匹配。
MATCH_PROTOCOL_VERSION = 2


def canonical_query(query: str) -> str:
    """规范化 query:保留参数与值的顺序语义,仅对参数名排序;空值保留。"""
    if not query:
        return ""
    from urllib.parse import parse_qsl, urlencode

    pairs = parse_qsl(query, keep_blank_values=True)
    return urlencode(sorted(pairs))


def canonical_body(data: bytes) -> str:
    """规范化请求体哈希输入:JSON 排序键;数组/文本顺序保留(§8.3)。"""
    try:
        parsed = json.loads(data)
    except ValueError:
        return hashlib.sha256(data).hexdigest()
    return hashlib.sha256(
        json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def body_hash(data: bytes) -> str:
    return canonical_body(data)


def redact(headers) -> dict:
    result = {}
    for key, value in headers.items():
        result[key] = "[REDACTED]" if key.lower() in REDACT_HEADERS else str(value)[:200]
    return result


class RecordingWriter:
    """一次 live run 的录制目录:interactions.jsonl + meta.json。"""

    def __init__(self, store: Path, recording_id: str, source_run_id: str | None = None):
        self.directory = store / "recordings" / recording_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.recording_id = recording_id
        self.index = 0
        if (self.directory / "interactions.jsonl").is_file():
            for line in (self.directory / "interactions.jsonl").read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.index += 1
        if not (self.directory / "meta.json").is_file():
            self._write_meta(sealed=False)

    def _write_meta(self, **patch) -> dict:
        meta_file = self.directory / "meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.is_file() else {}
        meta.update(
            recordingId=self.recording_id,
            formatVersion=1,
            matchProtocolVersion=MATCH_PROTOCOL_VERSION,
            **patch,
        )
        atomic_write_text(meta_file, json.dumps(meta, ensure_ascii=False, indent=2))
        return meta

    def append_interaction(
        self,
        request: httpx.Request,
        response: httpx.Response | None,
        error: str | None,
        objects: ObjectStore,
        trial_id=None,
        call_site: str | None = None,
    ) -> dict:
        request_body = request.content or b""
        entry: dict = {
            "index": self.index,
            "trialId": trial_id,
            "callSite": call_site,
            "timestamp": now(),
            "request": {
                "method": request.method,
                "url": str(request.url),
                "host": request.url.host,
                "port": request.url.port,
                "path": request.url.path,
                "query": canonical_query(request.url.query.decode("utf-8")),
                "bodyHash": body_hash(request_body),
                "body": self._inline(request_body),
                "headers": redact(request.headers),
            },
        }
        if response is not None:
            response_body = response.content or b""
            content_type = response.headers.get("content-type", "application/octet-stream")
            entry["response"] = {
                "status": response.status_code,
                "contentType": content_type,
                "bodyHash": sha256_bytes(response_body),
                "body": self._inline_text(response_body)
                if content_type.startswith(("text/", "application/json"))
                else None,
                "mediaRef": None
                if content_type.startswith(("text/", "application/json"))
                else objects.put(response_body, "bin", kind="other").artifactId,
                "headers": {k: response.headers.get(k) for k in KEEP_RESPONSE_HEADERS if k in response.headers},
            }
        else:
            entry["error"] = {"message": error}
        with (self.directory / "interactions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.index += 1
        return entry

    @staticmethod
    def _inline(data: bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return text if len(data) <= MAX_INLINE_BODY else None

    @staticmethod
    def _inline_text(data: bytes):
        if len(data) > MAX_INLINE_BODY:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def seal(self) -> dict:
        """完整性封存(§8.6):交互计数与账本哈希写入 meta;失败任务也可以是完整录制。"""
        interactions = self.directory / "interactions.jsonl"
        raw = interactions.read_bytes() if interactions.is_file() else b""
        digest = hashlib.sha256(raw).hexdigest()
        count = sum(1 for line in raw.decode("utf-8").splitlines() if line.strip())
        hosts = []
        for line in raw.decode("utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                host = item["request"]["host"]
                if host not in hosts:
                    hosts.append(host)
        meta = self._write_meta(
            sealed=True, sealedAt=now(), interactionCount=count, interactionsSha256=digest, hosts=hosts
        )
        return meta


class RecordingTransport(httpx.AsyncBaseTransport):
    """录制 transport:inner 可以是真实网络或测试用 stub 上游。"""

    def __init__(self, inner: httpx.AsyncBaseTransport, writer: RecordingWriter, objects: ObjectStore, context=None):
        self.inner = inner
        self.writer = writer
        self.objects = objects
        self.context = context  # CallContext:提供当前 trialId

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        trial_id = self.context.trial_id if self.context else None
        # 调用点由预算网关经 CallContext 传递;请求对象不再携带内部观测头,真实上游收到的请求保持干净。
        call_site = self.context.current_call_site if self.context else None
        try:
            response = await self.inner.handle_async_request(request)
            await response.aread()  # 读入内存以便记录;业务代码仍读 response.content
        except Exception as exc:
            self.writer.append_interaction(
                request, None, f"{type(exc).__name__}: {exc}", self.objects, trial_id,
                call_site=call_site,
            )
            raise
        self.writer.append_interaction(
            request, response, None, self.objects, trial_id, call_site=call_site
        )
        return response
