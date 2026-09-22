"""Repository(补充要求 §三.5):只做持久化与查询,不 commit、不决定业务流程。

事务边界由 Unit of Work 控制;唯一约束竞争经保存点翻译为 ConflictError(原因链保留),
外层事务保持可用。
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import ConflictError
from backend.infrastructure.orm import Asset, GenerationRequest, Job, Outbox, Workspace

_TERMINAL_STATUSES = {"completed", "failed", "unknown"}


class WorkspaceRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def ensure(self, workspace_id: uuid.UUID) -> Workspace:
        """幂等登记工作区;并发登记经保存点冲突恢复(100 并发受理验证)。"""
        row = await self._session.get(Workspace, workspace_id)
        if row is not None:
            return row
        async with self._session.begin_nested():
            self._session.add(Workspace(id=workspace_id))
            try:
                await self._session.flush()
            except IntegrityError:
                # 并发事务已登记同一工作区(对方已提交):回滚保存点后重读
                pass
        return await self._session.get(Workspace, workspace_id)


class JobRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, workspace_id: uuid.UUID, kind: str, params: dict) -> Job:
        job = Job(workspace_id=workspace_id, kind=kind, params=params)
        self._session.add(job)
        await self._session.flush()
        return job

    async def get(self, job_id: uuid.UUID) -> Job | None:
        return await self._session.get(Job, job_id)

    async def list_by_workspace(
        self, workspace_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> list[Job]:
        result = await self._session.execute(
            select(Job)
            .where(Job.workspace_id == workspace_id)
            .order_by(Job.created_at.desc(), Job.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars())

    async def transition(
        self,
        job_id: uuid.UUID,
        from_statuses: set[str],
        to_status: str,
        *,
        expected_version: int | None = None,
    ) -> bool:
        """条件状态转换:state_version 乐观并发;终态不被迟到事件改写(§六.7)。

        expected_version=None 时以库内当前版本为条件(仍受 from_statuses 终态保护)。
        """
        stmt = (
            update(Job)
            .where(Job.id == job_id, Job.status.in_(from_statuses))
            .values(
                status=to_status,
                state_version=Job.state_version + 1,
                updated_at=func.now(),
                finished_at=func.now() if to_status in _TERMINAL_STATUSES else None,
            )
        )
        if expected_version is not None:
            stmt = stmt.where(Job.state_version == expected_version)
        result = await self._session.execute(stmt)
        return result.rowcount == 1


    async def set_poll_schedule(
        self,
        job_id: uuid.UUID,
        *,
        external_task_id: str | None = None,
        next_poll_at,
        bump_attempt: bool = False,
    ) -> None:
        """登记上游任务 ID 与下次查询时间(分阶段执行 §七.1);失败退避由此推进。"""
        values = {"next_poll_at": next_poll_at, "updated_at": func.now()}
        if external_task_id is not None:
            values["external_task_id"] = external_task_id
        if bump_attempt:
            values["attempt_count"] = Job.attempt_count + 1
        await self._session.execute(update(Job).where(Job.id == job_id).values(**values))

    async def try_acquire_slot(self, job_id: uuid.UUID, *, scope: str, limit: int) -> bool:
        """原子领取模型执行名额(§八):单条 UPDATE 内联计数子查询,并发安全。

        领取失败(名额满)返回 False,任务保持现状态等待下次调度,不得降级为放行。

        READ COMMITTED 下 UPDATE 的 EPQ 子查询对其他行使用旧快照,并发领取会超发;
        先取事务级咨询锁把同 scope 的领取串行化,计数才准确(并发测试验证)。
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:scope)::bigint)"),
            {"scope": scope},
        )
        # 占用判定与业务状态解耦:slot_scope 非空即占用(领取时仍是 queued,
        # 若按 running 计数则并发事务互相不可见,导致超发——并发测试已验证)。
        used = (
            select(func.count())
            .select_from(Job)
            .where(Job.slot_scope == scope)
            .scalar_subquery()
        )
        stmt = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.slot_scope.is_(None),
                used < limit,
            )
            .values(slot_scope=scope, slot_acquired_at=func.now())
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount == 1

    async def release_slot(self, job_id: uuid.UUID) -> None:
        """按 job 自身为所有者释放名额(终态转换后调用;§八.6 防释放他人槽位)。"""
        await self._session.execute(
            update(Job)
            .where(Job.id == job_id, Job.slot_scope.is_not(None))
            .values(slot_scope=None, slot_acquired_at=None)
        )

    async def count_occupied_slots(self, scope: str) -> int:
        from sqlalchemy import func

        result = await self._session.execute(
            select(func.count()).select_from(Job).where(Job.slot_scope == scope)
        )
        return int(result.scalar_one())

    async def claim_due_polls(self, limit: int = 50):
        """调度器领取:running/queued 且到期的任务 → 派发对应事件(§七.2,原子)。

        返回 [(job_id, event_type)];领取与事件入队同一事务,由调用方 commit。
        """
        rows = (
            await self._session.execute(
                select(Job)
                .where(
                    Job.status.in_(["queued", "running"]),
                    Job.next_poll_at.is_not(None),
                    Job.next_poll_at <= func.now(),
                )
                .order_by(Job.next_poll_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
        plan = []
        for job in rows:
            event_type = "job.poll_due" if job.status == "running" else "job.created"
            # 领取即清空到期时间,避免 beat 在查询步推进 schedule 前重复入队。
            job.next_poll_at = None
            plan.append((job.id, event_type))
        await self._session.flush()
        return plan


class GenerationRequestRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def find(self, workspace_id: uuid.UUID, request_id: str) -> GenerationRequest | None:
        return (
            await self._session.execute(
                select(GenerationRequest).where(
                    GenerationRequest.workspace_id == workspace_id,
                    GenerationRequest.request_id == request_id,
                )
            )
        ).scalar_one_or_none()

    async def register(
        self,
        workspace_id: uuid.UUID,
        request_id: str,
        request_hash: str,
        params: dict,
        job_id: uuid.UUID,
    ) -> GenerationRequest:
        """幂等受理登记:同键不同参数返回 ConflictError(§五.6),并发竞争经保存点转换。"""
        existing = await self.find(workspace_id, request_id)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ConflictError(
                    "同一受理标识已用于不同参数",
                    details={"requestId": request_id, "jobId": str(existing.job_id)},
                )
            return existing
        record = GenerationRequest(
            workspace_id=workspace_id,
            request_id=request_id,
            request_hash=request_hash,
            params=params,
            job_id=job_id,
        )
        async with self._session.begin_nested():
            self._session.add(record)
            try:
                await self._session.flush()
            except IntegrityError as exc:
                raise ConflictError(
                    "同一受理标识已存在", details={"requestId": request_id}
                ) from exc
        return record


class AssetRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(
        self,
        workspace_id: uuid.UUID,
        *,
        kind: str,
        ext: str,
        storage_key: str,
        width: int = 0,
        height: int = 0,
        size: int = 0,
        sha256: str = "",
    ) -> Asset:
        asset = Asset(
            workspace_id=workspace_id,
            kind=kind,
            ext=ext,
            storage_key=storage_key,
            width=width,
            height=height,
            bytes=size,
            sha256=sha256,
        )
        self._session.add(asset)
        await self._session.flush()
        return asset

    async def count(self) -> int:
        from sqlalchemy import func

        result = await self._session.execute(select(func.count()).select_from(Asset))
        return int(result.scalar_one())


class OutboxRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def enqueue(
        self,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict,
        *,
        schema_version: str = "1",
    ) -> Outbox:
        event = Outbox(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            schema_version=schema_version,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def get(self, event_id: uuid.UUID) -> Outbox | None:
        return await self._session.get(Outbox, event_id)

    async def list_pending(self, limit: int = 100) -> list[Outbox]:
        result = await self._session.execute(
            select(Outbox)
            .where(Outbox.status == "pending")
            .order_by(Outbox.created_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def count_by_status(self, status: str) -> int:
        from sqlalchemy import func

        result = await self._session.execute(
            select(func.count()).select_from(Outbox).where(Outbox.status == status)
        )
        return int(result.scalar_one())

    async def claim_and_mark_publishing(self, limit: int = 50) -> list[Outbox]:
        """多投递器安全领取:SKIP LOCKED 原子领取并置 publishing(§五.2)。

        发布在事务外进行(§三.7);崩溃滞留的 publishing 由 reclaim_stale_publishing 回收。
        """
        stmt = (
            select(Outbox)
            .where(Outbox.status == "pending", Outbox.next_attempt_at <= func.now())
            .order_by(Outbox.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await self._session.execute(stmt)).scalars())
        claimed_at = datetime.now(timezone.utc)
        for row in rows:
            row.status = "publishing"
            row.claim_token = uuid.uuid4().hex
            # 领取时钟,reclaim_stale_publishing 按此判断滞留,而不是原来的到期时间。
            row.next_attempt_at = claimed_at
        await self._session.flush()
        return rows

    async def mark_publishing(self, event: Outbox) -> None:
        """标记发布中(由 claim_and_mark_publishing 批量使用;测试/修复工具亦可单独调用)。"""
        event.status = "publishing"
        event.claim_token = uuid.uuid4().hex
        event.next_attempt_at = datetime.now(timezone.utc)
        await self._session.flush()

    async def mark_published(self, event: Outbox) -> None:
        """仅在 Broker 确认发布后调用;确认前标记成功即为违例(§五.5)。"""
        event.status = "published"
        event.published_at = func.now()
        await self._session.flush()

    async def mark_failed(self, event: Outbox, *, backoff_s: int = 5) -> None:
        """发布失败(含无路由/ Broker 不可用):保持 pending 并退避。"""
        event.attempts += 1
        from backend.providers.policy import ProviderPolicy
        event.status = "failed" if event.attempts >= ProviderPolicy().outbox_max_attempts else "pending"
        event.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_s)
        await self._session.flush()

    async def reclaim_stale_publishing(self, older_than_s: int = 60) -> int:
        """回收滞留在 publishing 状态的过期事件(发布中崩溃),回到 pending。"""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_s)
        result = await self._session.execute(
            update(Outbox)
            .where(Outbox.status == "publishing", Outbox.next_attempt_at < cutoff)
            .values(status="pending")
        )
        return int(result.rowcount or 0)
