"""RabbitMQ 拓扑与发布(补充要求 §五)。

- 业务消息经 aivideo.jobs(topic)交换机;job.* 绑定到 quorum 队列(本地单节点开发,
  不宣称多节点容灾,§五.10);
- 处理失败经 DLX(aivideo.dlx)进入 aivideo.dead 死信队列,可查询恢复(§五.11/§九);
- 消息体只携带标识、schemaVersion 与必要追踪上下文(§五.7);
- 发布使用持久化消息(§五.9);确认发布由 Connection transport_options 的
  confirm_publish 提供(发布确认,§五.3);无路由(mandatory)抛 UnroutableError
  由调用方按失败处理(§五.8)。
"""

import socket
import time

from kombu import Exchange, Queue, binding

JOBS_EXCHANGE = Exchange("aivideo.jobs", type="topic", durable=True)
DLX_EXCHANGE = Exchange("aivideo.dlx", type="topic", durable=True)
GENERATION_QUEUE = Queue(
    "aivideo.jobs.generation",
    exchange=JOBS_EXCHANGE,
    bindings=[binding(JOBS_EXCHANGE, routing_key="job.created"),
              binding(JOBS_EXCHANGE, routing_key="job.poll_due"),
              # Compatibility for the pre-cutover pipeline only. Durable notifications
              # use job.changed on their own queue, never this legacy consumer.
              binding(JOBS_EXCHANGE, routing_key="job.updated")],
    durable=True,
    queue_arguments={"x-queue-type": "quorum", "x-dead-letter-exchange": "aivideo.dlx"},
)
DEAD_QUEUE = Queue(
    "aivideo.dead",
    exchange=DLX_EXCHANGE,
    routing_key="#",
    durable=True,
    queue_arguments={"x-queue-type": "quorum"},
)
PHASE_QUEUE = Queue(
    "aivideo.phase.events", exchange=JOBS_EXCHANGE, routing_key="execution.due", durable=True,
    queue_arguments={"x-queue-type": "quorum", "x-dead-letter-exchange": "aivideo.dlx"},
)
NOTIFICATION_QUEUE = Queue(
    "aivideo.notifications", exchange=JOBS_EXCHANGE, routing_key="job.changed", durable=True,
    queue_arguments={"x-queue-type": "quorum", "x-dead-letter-exchange": "aivideo.dlx"},
)


def declare_topology(connection) -> None:
    with connection.channel() as channel:
        for entity in (JOBS_EXCHANGE, DLX_EXCHANGE, GENERATION_QUEUE, DEAD_QUEUE, PHASE_QUEUE, NOTIFICATION_QUEUE):
            entity(channel).declare()


def event_body(
    *,
    event_id,
    event_type,
    schema_version,
    aggregate_type,
    aggregate_id,
    payload,
    occurred_at,
) -> dict:
    return {
        "eventId": str(event_id),
        "eventType": event_type,
        "schemaVersion": str(schema_version),
        "aggregateType": aggregate_type,
        "aggregateId": str(aggregate_id),
        "payload": payload,
        "occurredAt": occurred_at,
    }


def publish_event(connection, **kwargs) -> None:
    """确认发布(mandatory+持久化);无路由/确认失败抛异常,由投递器按失败退避。"""
    body = event_body(**kwargs)
    producer = connection.Producer()
    producer.publish(
        body,
        exchange=JOBS_EXCHANGE,
        routing_key=kwargs["event_type"],
        delivery_mode=2,  # persistent
        mandatory=True,
        retry=False,
        serializer="json",
    )


def drain_raw(connection, collector, limit=1, timeout_s=10.0, queue=None) -> int:
    """测试/工具用:从指定队列原样取消息并 ack,收集原始 JSON 体。"""
    queue_entities = [queue] if queue is not None else [GENERATION_QUEUE]
    count = 0

    def cb(body, message):
        nonlocal count
        collector(body)
        message.ack()
        count += 1

    with connection.Consumer(queue_entities, callbacks=[cb], accept=["json"]):
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
