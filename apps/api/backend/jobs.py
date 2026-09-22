"""Single-process FIFO scheduler. Tasks and SSE subscriptions are owned by app lifespan."""

import asyncio
import copy
import logging
import secrets
from collections import deque
from uuid import uuid4

from .common import now, read_json, write_json
from .errors import ErrorCode, Recovery, classify_exception
from .evaluation.tracing import start_trace_id
from .models import GenParams
from .traces import timestamp as parse_iso


class Jobs:
    def __init__(self, provider, history, file, traces, step_tracer=None):
        self.provider, self.history, self.file, self.traces = provider, history, file, traces
        self.step_tracer = step_tracer  # §9 步骤追踪;None 时为 NoopTracer 语义
        self.jobs = {}
        self.queue = deque()
        self.running = {}
        self.references = {}
        self.listeners = set()
        self.closing = False
        for job in read_json(file, {}).get("jobs", []):
            if not isinstance(job, dict) or not job.get("id") or not job.get("params"):
                continue
            if job.get("status") in {"queued", "running"}:
                previous_trace = job.get("traceId")
                if previous_trace and self.step_tracer is not None:
                    # 重启恢复建新 trace,并链接原 trace(§9.1,TRACE-02)。
                    recovery_id = start_trace_id()
                    self.step_tracer.recovery(recovery_id, previous_trace, job["id"], "服务重启恢复")
                    job["traceLinks"] = [previous_trace]
                    job["traceId"] = recovery_id
                if job.get("externalTaskId") and job.get("params", {}).get("kind") == "video":
                    # 已派发的付费任务:上游可能仍在执行,标记未知等待显式对账,绝不自动重提。
                    job.update(
                        status="unknown",
                        message="服务已重启,上游结果未知,请查询上游",
                        progress=None,
                    )
                else:
                    job.update(
                        status="failed",
                        error="服务已重启,任务中断",
                        message="服务已重启,任务中断",
                        progress=0,
                        finishedAt=now(),
                    )
            self.jobs[job["id"]] = job

    def flush(self):
        write_json(self.file, {"jobs": list(self.jobs.values())})

    def emit(self, event, data):
        for queue in self.listeners:
            if queue.full():
                # Disconnect lagging consumers; they reconnect and reconcile from snapshot.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(("overflow", {}))
            else:
                queue.put_nowait((event, copy.deepcopy(data)))

    def list(self, all_jobs=False, limit=30):
        items = sorted(self.jobs.values(), key=lambda j: j["createdAt"], reverse=True)
        return (
            items[:limit]
            if all_jobs
            else [j for j in items if j["status"] in {"queued", "running", "unknown"}]
        )

    def record_external(self, ident, task_id):
        """上游异步任务创建成功后立即落盘,重启后才能只查询不重提。"""
        job = self.jobs.get(ident)
        if job and job["status"] in {"running", "unknown"} and task_id:
            job["externalTaskId"] = str(task_id)[:128]
            self.flush()

    def resume(self, ident):
        """对账 unknown 任务:只查询上游既有任务并按结果收尾,由用户显式触发。"""
        job = self.jobs.get(ident)
        if not job or job["status"] != "unknown" or not job.get("externalTaskId"):
            return None
        job.update(status="running", startedAt=now(), message="查询上游任务")
        self.flush()
        self.emit("job", job)
        task = asyncio.create_task(self.run(ident, self.provider))
        self.running[ident] = task
        task.add_done_callback(lambda done, key=ident: self.release(key))
        return job

    def create(self, params, image=None, mask=None, client_ref=None, group=None, binding=None, trace_id=None):
        job = dict(
            id=str(uuid4()),
            status="queued",
            params=params.model_dump(exclude_none=True),
            batchCount=params.batchCount,
            hasInitImage=bool(image),
            progress=0,
            message="排队中",
            images=[],
            createdAt=now(),
        )
        if trace_id:
            job["traceId"] = trace_id
        if client_ref:
            job["clientRef"] = client_ref
        if group:
            job.update(group)
        if binding:
            job.update(binding)
        self.jobs[job["id"]] = job
        self.references[job["id"]] = (image, mask)
        self.queue.append(job["id"])
        self.flush()
        self.emit("job", job)
        self.pump()
        return job

    def pump(self):
        while not self.closing and self.queue and len(self.running) < self.provider.capacity:
            ident = self.queue.popleft()
            if self.jobs[ident]["status"] != "queued":
                continue
            # Capture provider: switching config must not alter an already running batch.
            task = asyncio.create_task(self.run(ident, self.provider))
            self.running[ident] = task
            task.add_done_callback(lambda done, key=ident: self.release(key))

    def release(self, ident):
        # Also runs if a task is cancelled before its coroutine gets its first turn.
        self.running.pop(ident, None)
        self.references.pop(ident, None)
        self.pump()

    def cancel(self, ident):
        job = self.jobs.get(ident)
        if not job:
            return None
        if job["status"] == "unknown":
            # 已派发到上游的任务:本地停止关注,并向厂商尽力取消;不宣称已停止或已退费。
            job.update(
                status="failed",
                error="已取消",
                message="已取消,上游停止结果待确认",
                finishedAt=now(),
            )
            self.emit("job", job)
            self.flush()
            task_id = job.get("externalTaskId")
            if task_id:
                try:
                    asyncio.create_task(self._cancel_external_quietly(task_id))
                except RuntimeError:
                    pass
            self.traces.terminal(job, self.provider)
            return job
        if job["status"] not in {"queued", "running"}:
            return None
        was_queued = job["status"] == "queued"
        upstream_pending = not was_queued and bool(job.get("externalTaskId"))
        if upstream_pending:
            # 结构化标记:本地停止等待,上游取消已请求但结果待确认;由 run 的取消分支消费。
            job["upstreamCancel"] = "pending"
        job.update(
            status="failed",
            error="已取消",
            message="已取消,上游停止结果待确认" if upstream_pending else "已取消",
            finishedAt=now(),
        )
        self.emit("job", job)
        if ident in self.running:
            self.running[ident].cancel()
        if upstream_pending:
            try:
                asyncio.create_task(self._cancel_external_quietly(job["externalTaskId"]))
            except RuntimeError:
                pass
        if was_queued:
            self.references.pop(ident, None)
            self.traces.terminal(job, self.provider)
        self.flush()
        return job

    async def _cancel_external_quietly(self, task_id):
        try:
            await self.provider.cancel_external(task_id)
        except Exception:
            logging.exception("上游任务取消失败")

    async def run(self, ident, provider):
        job = self.jobs[ident]
        if job["status"] == "failed":
            self.references.pop(ident, None)
            self.running.pop(ident, None)
            self.pump()
            return
        started_at = now()
        queue_ms = max(
            0, int((parse_iso(started_at) - parse_iso(job["createdAt"])).total_seconds() * 1000)
        )
        if self.step_tracer is not None and job.get("traceId"):
            self.step_tracer.step(job["traceId"], "queue.wait", jobId=ident, queueMs=queue_ms)
        job.update(status="running", startedAt=started_at, message="准备中")
        self.flush()
        self.emit("job", job)
        image, mask = self.references.get(ident, (None, None))
        params = GenParams.model_validate(job["params"])
        seed = params.seed if params.seed >= 0 else secrets.randbelow(2**31 - 1)
        try:
            for index in range(params.batchCount):

                def progress(value, message):
                    if job["status"] == "running":
                        job.update(
                            progress=min(
                                0.999, (index + max(0, min(1, value))) / params.batchCount
                            ),
                            message=message,
                        )
                        self.emit("job", job)

                # 云端异步任务:首个批次上报上游 task_id;重启对账时传入既有 task_id 只查询不重提。
                def report_external(task_id, _ident=ident):
                    self.record_external(_ident, task_id)

                external = report_external if index == 0 else None
                task_id = job.get("externalTaskId") if index == 0 else None
                if self.step_tracer is not None and job.get("traceId"):
                    self.step_tracer.step(
                        job["traceId"], "provider.submit", status="started", jobId=ident,
                        model=params.model, kind=params.kind, attempt=index,
                    )
                result = await provider.generate(
                    params, seed + index, progress, image, mask,
                    external=external, external_task_id=task_id,
                )
                if job["status"] != "running":
                    raise asyncio.CancelledError()
                # Serialized commit on event loop prevents cancellation between file and history ownership.
                record = self.history.save(
                    ident,
                    provider.name,
                    {**params.model_dump(exclude_none=True), "seed": seed + index},
                    result.data,
                    result.ext,
                )
                if self.step_tracer is not None and job.get("traceId"):
                    self.step_tracer.step(
                        job["traceId"], "artifact.persist", jobId=ident, imageId=record["id"],
                        bytes=len(result.data),
                    )
                job["images"].append(record)
                self.flush()
                self.emit("image", {"jobId": ident, "image": record})
            job.update(status="completed", progress=1, message="完成", finishedAt=now())
        except asyncio.CancelledError:
            # 上游取消请求已发出的取消:不宣称上游已停止,保留待确认语义。
            pending = job.pop("upstreamCancel", None) == "pending"
            message = "已取消,上游停止结果待确认" if pending else "已取消"
            job.update(status="failed", error="已取消", message=message, finishedAt=now())
            if self.step_tracer is not None and job.get("traceId"):
                self.step_tracer.step(job["traceId"], "provider.submit", status="cancelled", jobId=ident)
        except Exception as exc:
            if (
                (isinstance(exc, TimeoutError)
                 or classify_exception(exc)[1] == Recovery.RECONCILE)
                and job["status"] == "running"
                and (job.get("externalTaskId") or classify_exception(exc)[1] == Recovery.RECONCILE)
            ):
                # 轮询超时但上游付费任务已创建:结果未知,进入可对账状态,等待显式 resume,绝不自动重提。
                job.update(
                    status="unknown",
                    message="查询上游超时,上游结果未知,请对账",
                    progress=None,
                    errorCode=ErrorCode.UPSTREAM_UNKNOWN.value,
                    recovery=Recovery.RECONCILE.value,
                    finishedAt=now(),
                )
                if self.step_tracer is not None and job.get("traceId"):
                    self.step_tracer.step(
                        job["traceId"], "provider.submit", status="unknown", jobId=ident
                    )
            else:
                code, recovery = classify_exception(exc)
                message = "生成超时" if isinstance(exc, TimeoutError) else str(exc) or "生成失败"
                job.update(
                    status="failed",
                    error=message,
                    message=message,
                    errorCode=code.value,
                    recovery=recovery.value,
                    finishedAt=now(),
                )
                if self.step_tracer is not None and job.get("traceId"):
                    self.step_tracer.step(
                        job["traceId"], "provider.submit", status="failed", jobId=ident,
                        error=message[:200],
                    )
        finally:
            self.references.pop(ident, None)
            self.running.pop(ident, None)
            self.emit("job", job)
            self.traces.terminal(job, provider)
            finished = sorted(
                [j for j in self.jobs.values() if j["status"] not in {"queued", "running"}],
                key=lambda j: j.get("finishedAt", ""),
            )
            for old in finished[:-200]:
                if not old.get("requestId"):
                    self.jobs.pop(old["id"], None)
            try:
                self.flush()
            except OSError:
                logging.exception("任务快照写入失败")
            self.pump()

    async def close(self):
        self.closing = True
        tasks = list(self.running.values())
        for job in list(self.list()):
            self.cancel(job["id"])
        await asyncio.gather(*tasks, return_exceptions=True)
        self.flush()
