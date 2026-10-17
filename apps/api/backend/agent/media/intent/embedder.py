"""仅访问显式配置的本机/内网 embedding 侧车，不调用生成厂商。"""
import ipaddress
import math
from urllib.parse import urlparse
import httpx


class EmbedError(RuntimeError):
    pass


def validate_vectors(vectors, count):
    if not isinstance(vectors, list) or len(vectors) != count or not vectors:
        raise EmbedError('向量数量不匹配')
    dimension = len(vectors[0]) if isinstance(vectors[0], list) else 0
    if not dimension or dimension > 8192:
        raise EmbedError('向量维度无效')
    for vector in vectors:
        if (not isinstance(vector, list) or len(vector) != dimension or
                any(type(x) not in (float, int) or not math.isfinite(x) for x in vector) or
                sum(x*x for x in vector) <= 0):
            raise EmbedError('向量内容无效')
    return vectors


class HTTPEmbedder:
    def __init__(self, url, model_id, timeout=0.8):
        parsed = urlparse(url)
        host = parsed.hostname or ''
        try:
            private = ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
        except ValueError:
            private = host in {'localhost', 'intent-embedder'} or host.endswith(('.internal', '.local'))
        if parsed.scheme not in {'http', 'https'} or not private or parsed.username or parsed.password:
            raise EmbedError('向量侧车必须使用本机或内网地址')
        if not model_id or model_id == 'pending':
            raise EmbedError('必须固定 embedding 模型版本')
        self.url, self.model_id, self.timeout = url, model_id, timeout

    async def embed(self, texts):
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
                response = await client.post(self.url, json={'model': self.model_id, 'input': texts})
                response.raise_for_status()
                payload = response.json()
                if payload.get('model') != self.model_id:
                    raise EmbedError('侧车返回的模型版本不一致')
                return validate_vectors([item['embedding'] for item in payload['data']], len(texts))
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmbedError('向量侧车不可用或响应无效') from exc
