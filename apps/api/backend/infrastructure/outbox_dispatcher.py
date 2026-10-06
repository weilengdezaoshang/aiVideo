"""Outbox 投递器(补充要求 §五)。

- 多实例安全:claim_and_mark_publishing 用 FOR UPDATE SKIP LOCKED 原子领取并置
  publishing;发布在数据库事务外执行(§三.7),发布中崩溃的滞留行由
  reclaim_stale_publishing 回收(§五.7);
- 只有 RabbitMQ 确认发布后才标记 published(§五.5);无路由/Broker 不可用一律
  失败退避,事件保持 pending(§五.2/§五.8);
- 重复发布可能发生(确认丢失),由消费端业务幂等消化(§五.6)。
"""

import asyncio

from kombu import Connection

from backend.infrastructure.messaging import declare_topology, publish_event


class OutboxDispatcher:
    def __init__(self, settings, broker_url):
        self._settings = settings
        self._broker_url = broker_url

    def dispatch_once(self, limit: int = 50, *, timeout_s: float = 10, backoff_s: int = 5) -> dict:
        """领取并投递一批到期事件;返回 {published, failed}。"""
        result = {"published": 0, "failed": 0}
        rows = asyncio.run(self._claim(limit))
        if not rows:
            return result
        published_ids: list[str] = []
        failed_ids: list[str] = []
        try:
            with Connection(
                self._broker_url,
                transport_options={"confirm_publish": True, "connect_timeout": timeout_s},
            ) as conn:
                declare_topology(conn)
                for row in rows:
                    try:
                        publish_event(
                            conn,
                            event_id=row["id"],
                            event_type=row["event_type"],
                            schema_version=row["schema_version"],
                            aggregate_type=row["aggregate_type"],
                            aggregate_id=row["aggregate_id"],
                            payload=row["payload"],
                            occurred_at=row["created_at"].isoformat()
                            if row["created_at"]
                            else "",
                        )
                        published_ids.append(row["id"])
                    except Exception:
                        failed_ids.append(row["id"])
        except Exception:
            # 连接或拓扑失败:整批退避(§五.2 broker 不可用时事件保持 pending)
            failed_ids.extend(r["id"] for r in rows if r["id"] not in published_ids)
        asyncio.run(self._settle(published_ids, failed_ids, backoff_s,
                                {r["id"]: r["claim_token"] for r in rows}))
        result["published"] = len(published_ids)
        result["failed"] = len(failed_ids)
        return result

    def reclaim_stale_publishing(self, older_than_s: int = 60) -> int:
        """回收发布中崩溃的滞留事件(§五.7),由周期任务(§七)定期调用。"""
        return asyncio.run(self._reclaim(older_than_s))

    async def _claim(self, limit: int):
        from backend.infrastructure.uow import AsyncUnitOfWork

        async with AsyncUnitOfWork(self._settings) as uow:
            rows = await uow.outbox.claim_and_mark_publishing(limit=limit)
            await uow.commit()
            return [
                dict(
                    id=str(r.id),
                    claim_token=r.claim_token,
                    event_type=r.event_type,
                    schema_version=r.schema_version,
                    aggregate_type=r.aggregate_type,
                    aggregate_id=r.aggregate_id,
                    payload=r.payload or {},
                    created_at=r.created_at,
                )
                for r in rows
            ]

    async def _settle(self, published_ids, failed_ids, backoff_s: int, tokens=None):
        from backend.infrastructure.uow import AsyncUnitOfWork

        async with AsyncUnitOfWork(self._settings) as uow:
            for eid in published_ids:
                row = await self._locked_event(uow, eid)
                if row is not None and row.status == "publishing" and tokens and row.claim_token == tokens.get(eid):
                    await uow.outbox.mark_published(row)
            for eid in failed_ids:
                row = await self._locked_event(uow, eid)
                if row is not None and row.status == "publishing" and tokens and row.claim_token == tokens.get(eid):
                    await uow.outbox.mark_failed(row, backoff_s=backoff_s)
            await uow.commit()

    async def _locked_event(self, uow, eid):
        import uuid
        from sqlalchemy import select
        from backend.infrastructure.orm import Outbox

        return (await uow.session.execute(select(Outbox).where(
            Outbox.id == uuid.UUID(str(eid))).with_for_update())).scalar_one_or_none()

    async def _reclaim(self, older_than_s: int) -> int:
        from backend.infrastructure.uow import AsyncUnitOfWork

        async with AsyncUnitOfWork(self._settings) as uow:
            count = await uow.outbox.reclaim_stale_publishing(older_than_s)
            await uow.commit()
            return count
