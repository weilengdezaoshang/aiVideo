"""严格回放(技术方案 §8.3-8.5,验收 REPLAY-01..08)。

- 只接受 sealed 录制;执行业务代码,每个外部请求按"匹配协议 v2"消费对应录制交互:
  method + host + port + path + 规范化 query + 规范化 body 哈希,并按 trialId 分桶,
  避免同形请求跨 trial 错配(§8.3)。
- 动态字段的忽略必须显式配置(ignore_body_fields / ignore_query_params),且只做
  字段级排除,不做宽泛删除;配置进入回放证据,可审计。
- 未匹配立即失败(REPLAY_REQUEST_MISMATCH),给出与同路径候选的字段级 diff;
  绝不自动转真实调用,也绝不做近似/子串匹配。
- 录制中记录的错误(超时/断连)按原样重放;非 2xx 状态按原状态返回(REPLAY-05)。
- host 不在录制清单内 → NETWORK_FORBIDDEN(禁网护栏,REPLAY-06);护栏触发即
  记入 observed_new_calls,作为实际观测的新增出口证据。
- 回放结束后 unconsumed() 暴露缺失消费的交互;runner 据此判定 run 失败(§8.3)。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import httpx

from .recorder import MATCH_PROTOCOL_VERSION


class ReplayMismatch(ValueError):
    code = "REPLAY_REQUEST_MISMATCH"


class NetworkForbidden(ValueError):
    code = "NETWORK_FORBIDDEN"


class RecordingIncomplete(ValueError):
    code = "RECORDING_INCOMPLETE"


@dataclass
class RecordedInteraction:
    index: int
    trialId: str | None
    callSite: str | None
    method: str
    host: str
    port: int | None
    path: str
    query: str
    url: str
    request_body: bytes
    status: int | None
    content_type: str
    response_body: bytes | None
    media_ref: str | None
    error: str | None


def _error_response(status: int, content: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"content-type": content_type} if content_type else None,
        content=content,
    )


def normalize_body(body: bytes, ignore_fields: list[str] | None = None) -> str:
    """body 规范化哈希:JSON 键排序;显式列出的顶层字段先剔除,其余保持原语义。

    ignore_fields 只接受字段名(精确匹配,不支持通配),避免宽泛删除导致误匹配。
    """
    ignore = set(ignore_fields or [])

    def strip(value):
        if isinstance(value, dict) and ignore:
            return {k: v for k, v in value.items() if k not in ignore}
        return value

    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        if ignore:
            raise ValueError("ignore_body_fields 只支持 JSON 请求体") from None
        return hashlib.sha256(body).hexdigest()
    stripped = strip(parsed)
    return hashlib.sha256(
        json.dumps(stripped, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def normalize_query(query: str, ignore_params: list[str] | None = None) -> str:
    ignore = set(ignore_params or [])
    pairs = [pair for pair in parse_qsl(query, keep_blank_values=True) if pair[0] not in ignore]
    return urlencode(sorted(pairs))


class RecordingReader:
    def __init__(self, store: Path, recording_id: str, objects=None):
        self.directory = store / "recordings" / recording_id
        meta_file = self.directory / "meta.json"
        interactions_file = self.directory / "interactions.jsonl"
        if not meta_file.is_file() or not interactions_file.is_file():
            raise RecordingIncomplete(f"录制不存在或不完整:{recording_id}")
        self.meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if not self.meta.get("sealed"):
            raise RecordingIncomplete(f"录制未封存,不能严格回放:{recording_id}")
        raw = interactions_file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.meta.get("interactionsSha256"):
            raise RecordingIncomplete(f"录制账本哈希不一致,可能被篡改:{recording_id}")
        self.recording_id = recording_id
        self.match_protocol_version = int(self.meta.get("matchProtocolVersion", 1))
        self.interactions: list[RecordedInteraction] = []
        objects = objects
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            request = item["request"]
            response = item.get("response")
            response_body = b""
            if response is not None:
                if response.get("body") is not None:
                    response_body = response["body"].encode("utf-8")
                elif response.get("mediaRef") and objects is not None:
                    try:
                        _, response_body = objects.get(response["mediaRef"])
                    except (FileNotFoundError, ValueError) as exc:
                        raise RecordingIncomplete(
                            f"录制引用的媒体缺失或损坏:{response['mediaRef']}({exc})(REPLAY-07)"
                        ) from exc
                elif response.get("mediaRef"):
                    raise RecordingIncomplete(
                        f"录制引用的媒体缺失:{response['mediaRef']}(REPLAY-07)"
                    )
            self.interactions.append(
                RecordedInteraction(
                    index=item["index"],
                    trialId=item.get("trialId"),
                    callSite=item.get("callSite"),
                    method=request["method"],
                    host=request["host"],
                    port=request.get("port"),
                    path=request["path"],
                    query=request.get("query", ""),
                    url=request["url"],
                    request_body=request.get("body", "").encode("utf-8") if request.get("body") else b"",
                    status=response["status"] if response else None,
                    content_type=(response or {}).get("contentType", "application/json"),
                    response_body=response_body if response else None,
                    media_ref=(response or {}).get("mediaRef"),
                    error=(item.get("error") or {}).get("message"),
                )
            )

    @property
    def hosts(self) -> set[str]:
        return set(self.meta.get("hosts", []))


class ReplayTransport(httpx.AsyncBaseTransport):
    """严格回放 transport:每个请求只消费对应录制交互,拒绝一切新出口。"""

    def __init__(
        self,
        recording: RecordingReader,
        context=None,
        ignore_body_fields: list[str] | None = None,
        ignore_query_params: list[str] | None = None,
    ):
        self.recording = recording
        self.context = context
        self.protocol_version = MATCH_PROTOCOL_VERSION
        self.ignore_body_fields = list(ignore_body_fields or [])
        self.ignore_query_params = list(ignore_query_params or [])
        self.matched: list[RecordedInteraction] = []
        self.observed_new_calls: list[tuple[str, str]] = []  # 实际观测的录制外出口
        # 主索引:按录制时的 trialId 分桶(避免同形请求跨 trial 错配);
        # 兼容索引:旧录制无 trialId 的交互进入全局桶,显式回退,不做宽泛匹配。
        self._by_key: dict[tuple, deque[RecordedInteraction]] = defaultdict(deque)
        self._by_key_global: dict[tuple, deque[RecordedInteraction]] = defaultdict(deque)
        for item in recording.interactions:
            key = (
                item.trialId,
                item.method,
                item.host,
                item.path,
                normalize_query(item.query, self.ignore_query_params),
                normalize_body(item.request_body, self.ignore_body_fields),
            )
            self._by_key[key].append(item)
            if item.trialId is None:
                # 旧格式录制(无 trialId)进入全局桶;v2 录制按 trial 严格隔离。
                self._by_key_global[(None,) + key[1:]].append(item)

    def _key(self, request: httpx.Request):
        trial_id = self.context.match_trial_id or self.context.trial_id if self.context else None
        return (
            trial_id,
            request.method,
            request.url.host,
            request.url.path,
            normalize_query(request.url.query.decode("utf-8"), self.ignore_query_params),
            normalize_body(request.content or b"", self.ignore_body_fields),
        )

    @staticmethod
    def _field_diff(expected: bytes, actual: bytes) -> str:
        try:
            expected_json = json.loads(expected) if expected else None
            actual_json = json.loads(actual) if actual else None
        except ValueError:
            return "请求体不是 JSON,原始哈希不同"
        if not isinstance(expected_json, dict) or not isinstance(actual_json, dict):
            if expected_json != actual_json:
                return (
                    f"录制体={json.dumps(expected_json, ensure_ascii=False)[:120]} "
                    f"实际体={json.dumps(actual_json, ensure_ascii=False)[:120]}"
                )
            return "语义等价但规范化哈希不同(数组顺序等)"
        diffs = []
        for key in sorted(set(expected_json) | set(actual_json)):
            if json.dumps(expected_json.get(key), ensure_ascii=False, sort_keys=True) != json.dumps(
                actual_json.get(key), ensure_ascii=False, sort_keys=True
            ):
                diffs.append(
                    f"字段 {key}: 录制={json.dumps(expected_json.get(key), ensure_ascii=False)[:120]} "
                    f"实际={json.dumps(actual_json.get(key), ensure_ascii=False)[:120]}"
                )
        return ";".join(diffs) or "语义等价但规范化哈希不同(数组顺序等)"

    def _candidate_diff(self, request: httpx.Request) -> str:
        candidates = [
            item
            for item in self.recording.interactions
            if item.method == request.method and item.path == request.url.path
        ]
        details = []
        for item in candidates[:3]:
            detail = self._field_diff(item.request_body, request.content or b"")
            if item.query != request.url.query.decode("utf-8"):
                detail += f";query 录制={item.query or '(空)'} 实际={request.url.query.decode('utf-8') or '(空)'}"
            details.append(detail)
        return ";".join(details)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host not in self.recording.hosts:
            self.observed_new_calls.append(
                (request.method, f"{request.url.host}{request.url.path}")
            )
            raise NetworkForbidden(
                f"[NETWORK_FORBIDDEN] 严格回放禁止访问录制之外的出口:"
                f"{request.url.host}{request.url.path}(REPLAY-06)"
            )
        key = self._key(request)
        queue = self._by_key.get(key)
        if queue is None and key[0] is not None:
            # trial 桶未命中时回退到旧格式(无 trialId)的全局桶;
            # v2 录制全部交互带 trialId,不会进入该桶,保持跨 trial 隔离。
            queue = self._by_key_global.get((None,) + key[1:])
        if not queue:
            raise ReplayMismatch(
                f"[REPLAY_REQUEST_MISMATCH] 实际请求与录制不一致"
                f"(method={request.method} path={request.url.path} "
                f"trial={self.context.trial_id if self.context else None}):"
                f"{self._candidate_diff(request) or '录制中没有该调用'}"
            )
        item = queue.popleft()
        self.matched.append(item)
        if item.error:
            raise RuntimeError(f"录制中的故障被重放:{item.error}")
        return _error_response(item.status, item.response_body or b"", item.content_type)

    def unconsumed(self) -> list[RecordedInteraction]:
        consumed = {item.index for item in self.matched}
        return [item for item in self.recording.interactions if item.index not in consumed]
