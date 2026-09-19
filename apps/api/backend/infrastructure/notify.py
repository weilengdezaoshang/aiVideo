"""跨实例事件通知(补充要求 §十.12):Redis Pub/Sub 发布 + 订阅桥。

- 任务状态经 Outbox 消费者发布到 Redis 频道;各 API 实例订阅并推送给本地 SSE
  订阅者,实现跨实例通知;
- 通知只携带标识与状态字段(不含完整快照、媒体与敏感参数);
- Pub/Sub 丢失不影响正确性:以数据库/缓存查询对账恢复(§十四.2 的降级语义)。
"""

import json

import redis


class RedisNotify:
    def __init__(self, url, channel="aivideo:notify:jobs"):
        self._client = redis.Redis.from_url(url, decode_responses=True)
        self._channel = channel

    def publish(self, event: dict) -> None:
        """发布通知;失败由调用方容忍(Pub/Sub 尽力送达,正确性靠查询对账)。"""
        self._client.publish(self._channel, json.dumps(event, ensure_ascii=False))

    def subscribe(self):
        """返回已订阅频道的 pubsub 对象(get_message 消费);close() 释放。"""
        pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(self._channel)
        return pubsub
