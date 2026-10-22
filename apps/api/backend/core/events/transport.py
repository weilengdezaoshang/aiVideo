"""RabbitMQ is a wake-up bell; PostgreSQL owns ordering, retries and effects."""
import asyncio
from datetime import timedelta
import socket
import uuid

from kombu import Connection, Queue
from sqlalchemy import and_, exists, or_, select, update

from backend.core.orm import CoreEvent
from backend.infrastructure.messaging import JOBS_EXCHANGE
from .contracts import ClaimCheck, partition
from .orm import EventAlert, EventReceipt
from .store import accept, alert, db_now
from .integrity import quarantined, completion_proof
from .locking import bounded, locked, LockBusy, lock_error


class RabbitTransport:
    def __init__(self, url, settings):
        self.url, self.settings = url, settings
        self.queues = [Queue(f"aivideo.core.events.{n}", exchange=JOBS_EXCHANGE,
            routing_key=f"core.event.{n}", durable=True,
            queue_arguments={"x-queue-type": "quorum"}) for n in range(settings.partitions)]

    def connection(self):
        return Connection(self.url, connect_timeout=self.settings.broker_timeout_seconds,
            transport_options={"confirm_publish": True})

    def declare(self, connection):
        with connection.channel() as channel:
            JOBS_EXCHANGE(channel).declare()
            for queue in self.queues:
                queue(channel).declare()

    def send(self, claim):
        claim = ClaimCheck.model_validate(claim)
        with self.connection() as connection:
            self.declare(connection)
            connection.Producer().publish(claim.model_dump(mode="json"), exchange=JOBS_EXCHANGE,
                routing_key=f"core.event.{partition(claim.run_id, self.settings.partitions)}",
                delivery_mode=2, mandatory=True, retry=False, serializer="json",
                timeout=self.settings.broker_timeout_seconds,
                confirm_timeout=self.settings.broker_timeout_seconds)

    def receive(self, persist):
        """ACK only after persist returns (a committed inbox transaction)."""
        count = 0
        def callback(body, message):
            nonlocal count
            try:
                persist(body)
            except ValueError:
                message.reject(requeue=False)
                raise
            except Exception:
                message.reject(requeue=True)
                raise
            message.ack()
            count += 1
        with self.connection() as connection:
            self.declare(connection)
            with connection.Consumer(self.queues, callbacks=[callback], accept=["json"],
                                     prefetch_count=self.settings.prefetch):
                while count < self.settings.batch_size:
                    try:
                        connection.drain_events(timeout=self.settings.poll_seconds)
                    except socket.timeout:
                        break
        return count


class Publisher:
    def __init__(self, factory, settings, transport, after_send=None):
        self.factory, self.settings, self.transport = factory, settings, transport
        self.after_send = after_send

    async def claim(self):
        async with self.factory.begin() as tx:
            await bounded(tx, self.settings)
            stamp = await db_now(tx)
            event = await tx.scalar(select(CoreEvent).where(~quarantined(CoreEvent.id),
                ~completion_proof(CoreEvent.id, CoreEvent.run_id, CoreEvent.run_sequence), or_(
                and_(CoreEvent.delivery_status.in_(["pending", "failed"]),
                    or_(CoreEvent.next_attempt_at.is_(None), CoreEvent.next_attempt_at <= stamp)),
                and_(CoreEvent.delivery_status == "claimed", CoreEvent.lease_until <= stamp),
                and_(CoreEvent.delivery_status == "published",
                    CoreEvent.published_at <= stamp - timedelta(seconds=self.settings.delivery_visibility_seconds),
                    ~exists(select(EventReceipt.event_id).where(EventReceipt.event_id == CoreEvent.id)))))
                .order_by(CoreEvent.created_at, CoreEvent.id).limit(1).with_for_update(skip_locked=True))
            if event is None:
                return None
            event.delivery_status, event.claim_token = "claimed", uuid.uuid4()
            event.lease_until = stamp + timedelta(seconds=self.settings.publish_lease_seconds)
            event.attempts += 1
            return ClaimCheck(event_id=event.id, run_id=event.run_id), event.claim_token, event.attempts

    async def once(self):
        claim = await self.claim()
        if claim is None:
            return False
        body, token, attempts = claim
        error = None
        try:
            await asyncio.to_thread(self.transport.send, body)
            if self.after_send:
                await self.after_send(body)
        except Exception as exc:
            error = exc
        async with self.factory.begin() as tx:
            await bounded(tx, self.settings)
            event = await locked(tx, CoreEvent, body.event_id)
            if event is None:
                from .integrity import quarantine
                await quarantine(tx, event_id=body.event_id, run_id=body.run_id,
                    code="missing_event", phase="publish_settle")
                return False
            stamp = await db_now(tx)
            values = {"delivery_status": "published" if error is None else "failed",
                "claim_token": None, "lease_until": None,
                "next_attempt_at": None if error is None else stamp + timedelta(seconds=self.settings.delay(attempts))}
            if error is None:
                values["published_at"] = stamp
            result = await tx.execute(update(CoreEvent).where(CoreEvent.id == body.event_id,
                CoreEvent.claim_token == token).values(**values))
            if result.rowcount and error is None:
                await tx.execute(update(EventAlert).where(EventAlert.event_id == body.event_id,
                    EventAlert.kind == "publish_failure", EventAlert.resolved_at.is_(None)).values(resolved_at=stamp))
            if result.rowcount and error is not None and attempts >= self.settings.publish_alert_attempts:
                await alert(tx, "publish_failure", body.event_id, f"publish:{body.event_id}")
        return error is None

    async def tick(self):
        count = 0
        for _ in range(self.settings.batch_size):
            try:
                if not await self.once():
                    break
            except Exception as exc:
                if not isinstance(exc, LockBusy) and not lock_error(exc):
                    raise
                break
            count += 1
        return count


class Receiver:
    def __init__(self, factory, transport):
        self.factory, self.transport = factory, transport

    async def persist(self, body):
        try:
            async with self.factory.begin() as tx:
                await bounded(tx, self.transport.settings)
                return await accept(tx, body)
        except ValueError:
            # Bad schemas are terminal only after their diagnostic dead letter commits.
            from .integrity import quarantine
            def ident(key):
                try:
                    return uuid.UUID(str(body.get(key))) if isinstance(body, dict) else None
                except ValueError:
                    return None
            event_id, run_id = ident("event_id"), ident("run_id")
            async with self.factory.begin() as tx:
                await bounded(tx, self.transport.settings)
                await quarantine(tx, event_id=event_id, run_id=run_id, code="invalid_claim", phase="receive",
                    source_key=f"claim:invalid:{event_id}:{run_id}")
            return event_id

    async def tick(self):
        loop = asyncio.get_running_loop()
        def persist(body):
            return asyncio.run_coroutine_threadsafe(self.persist(body), loop).result()
        return await asyncio.to_thread(self.transport.receive, persist)
