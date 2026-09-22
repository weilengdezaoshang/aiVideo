"""业务消息消费(补充要求 §五.11)。

消费失败 → reject(requeue=False) → DLX 死信队列(§九),不无限重投;
成功路径由回调负责 ack。消费端不复制业务状态判断,幂等由应用用例的
条件状态转换保证(§五.6)。
"""

import socket
import time

from backend.infrastructure.messaging import GENERATION_QUEUE

# 识别的事件 schema 版本(§十二.4):未识别版本不得当正常业务执行,进死信待处理。
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1"})


def drain_generation_events(connection, on_message, limit=10, timeout_s=5.0, on_error=None) -> int:
    """消费至多 limit 条成功消息;on_message(body, message) 完成业务动作并 ack。"""
    count = 0

    def cb(body, message):
        nonlocal count
        if str(body.get("schemaVersion")) not in SUPPORTED_SCHEMA_VERSIONS:
            # 未识别版本:拒绝处理并进入死信,不做业务执行(§十二.4)
            if on_error is not None:
                on_error(body, ValueError(f"未识别的消息版本:{body.get('schemaVersion')}"))
            message.reject(requeue=False)
            return
        try:
            on_message(body, message)
        except Exception as exc:
            if on_error is not None:
                on_error(body, exc)
            message.reject(requeue=False)  # 受控进入死信,不无限重投
            return
        count += 1

    with connection.Consumer([GENERATION_QUEUE], callbacks=[cb], accept=["json"]):
        deadline = time.monotonic() + timeout_s
        while count < limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                connection.drain_events(timeout=min(remaining, 1.0))
            except socket.timeout:
                break
    return count
