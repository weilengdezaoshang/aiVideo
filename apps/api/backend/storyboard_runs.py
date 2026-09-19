"""Sequential media generation built on the existing idempotent node submission path."""

import asyncio
import hashlib
import json
from copy import deepcopy
from uuid import UUID, uuid4

from fastapi import HTTPException

from .common import now, read_json, write_json

ACTIVE_NODE_RUN_STATUSES = ("submitting", "queued", "running", "uncertain")


class StoryboardRuns:
    def __init__(self, data, documents, jobs, submit):
        self.root = data / "storyboard-runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.documents, self.jobs, self.submit = documents, jobs, submit
        self.runs, self.tasks = {}, {}
        for path in self.root.glob("*.json"):
            run = read_json(path, None)
            if not isinstance(run, dict) or run.get("id") != path.stem:
                continue
            try:
                UUID(path.stem)
            except ValueError:
                continue
            if run.get("status") == "running":
                run.update(status="paused", message="服务已重启，请检查当前镜头结果后继续")
                self.save(run)
            self.runs[path.stem] = run

    def save(self, run):
        run["updatedAt"] = now()
        write_json(self.root / f"{run['id']}.json", run)

    def list_for_document(self, doc_id):
        """按项目发现执行记录：恢复不依赖浏览器本地存储。"""
        runs = sorted(
            (run for run in self.runs.values() if run.get("documentId") == doc_id),
            key=lambda run: (run.get("createdAt", ""), run["id"]),
            reverse=True,
        )
        return [
            {
                key: run.get(key)
                for key in (
                    "id",
                    "documentId",
                    "status",
                    "index",
                    "message",
                    "createdAt",
                    "updatedAt",
                )
            }
            | {"total": len(run.get("steps", []))}
            for run in runs[:20]
        ]

    def start(self, ident, doc_id, steps):
        try:
            if str(UUID(ident)) != ident:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("执行请求标识无效") from None
        if not isinstance(steps, list) or not 1 <= len(steps) <= 200:
            raise ValueError("执行镜头数量必须为 1 到 200")
        seen, requests = set(), set()
        doc = self.documents.get(doc_id)
        if not doc:
            raise HTTPException(404, "项目不存在")
        for step in steps:
            if not isinstance(step, dict) or set(step) != {"shotId", "draft", "request"}:
                raise ValueError("镜头执行数据无效")
            raw = step["request"]
            if not isinstance(raw, dict) or raw.get("documentId") != doc_id:
                raise ValueError("镜头请求不属于当前项目")
            rid, node_id = raw.get("requestId"), raw.get("clientRef")
            if not isinstance(node_id, str) or not node_id or node_id in seen:
                raise ValueError("执行节点标识无效或重复")
            try:
                if str(UUID(rid)) != rid or rid in requests:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError("镜头请求标识无效或重复") from None
            seen.add(node_id)
            requests.add(rid)
            if not isinstance(step["draft"], dict) or not isinstance(step["shotId"], str):
                raise ValueError("镜头快照无效")
        fingerprint = hashlib.sha256(
            json.dumps([doc_id, steps], sort_keys=True).encode()
        ).hexdigest()
        if ident in self.runs:
            if self.runs[ident]["fingerprint"] != fingerprint:
                raise HTTPException(409, "同一执行标识不能更改镜头")
            return self.get(ident)
        if any(
            run["documentId"] == doc_id and run["status"] in ("running", "paused")
            for run in self.runs.values()
        ):
            raise HTTPException(409, "当前项目已有执行记录，请继续或取消该记录")
        if any(
            job.get("documentId") == doc_id and job.get("requestId") in requests
            for job in self.jobs.jobs.values()
        ):
            raise HTTPException(409, "镜头请求已用于其他生成，请使用新的执行请求")
        for step in steps:
            self.check_shot(doc, step)
        run = dict(
            id=ident,
            documentId=doc_id,
            steps=deepcopy(steps),
            index=0,
            status="running",
            message="开始顺序生成",
            fingerprint=fingerprint,
            createdAt=now(),
        )
        self.save(run)
        self.runs[ident] = run
        self.launch(run)
        return self.get(ident)

    def find_request(self, doc_id, request_id):
        return next(
            (
                job
                for job in self.jobs.jobs.values()
                if job.get("documentId") == doc_id and job.get("requestId") == request_id
            ),
            None,
        )

    def check_shot(self, doc, step):
        node_id = step["request"]["clientRef"]
        shot = next(
            (
                shot
                for shot in (doc or {}).get("storyboard", {}).get("shots", [])
                if shot["id"] == step["shotId"]
            ),
            None,
        )
        node = (doc or {}).get("objects", {}).get(node_id)
        if not shot or shot.get("locked") or shot.get("nodeId") != node_id or not node:
            raise ValueError("镜头已锁定、删除或更改关联，请检查后继续")
        raw = step["request"]
        draft = step["draft"]
        references = [
            item.get("assetId") for item in draft.get("references", []) or [] if isinstance(item, dict)
        ]
        if (
            node.get("kind") not in ("image", "video")
            or raw.get("kind") != node.get("kind")
            or any(raw.get(key) != draft.get(key) for key in ("prompt", "model", "durationSec"))
            or list(raw.get("referenceAssetIds", []) or []) != references
        ):
            raise ValueError("生成请求与镜头快照不一致（含参考图）")
        run = node.get("nodeRun")
        if (
            isinstance(run, dict)
            and run.get("status") in ACTIVE_NODE_RUN_STATUSES
            and run.get("requestId") != raw.get("requestId")
        ):
            raise ValueError("节点已有进行中的生成任务，请先处理该任务")
        if node.get("nodeDraft") != draft:
            raise ValueError("镜头参数已修改，请取消执行后重新开始")

    def launch(self, run):
        task = asyncio.create_task(self.execute(run))
        self.tasks[run["id"]] = task

        def release(done):
            if self.tasks.get(run["id"]) is done:
                self.tasks.pop(run["id"], None)

        task.add_done_callback(release)

    async def execute(self, run):
        try:
            while run["status"] == "running" and run["index"] < len(run["steps"]):
                step = run["steps"][run["index"]]
                job = self.jobs.jobs.get(step.get("jobId"))
                if job is None:
                    self.check_shot(self.documents.get(run["documentId"]), step)
                    response = await self.submit(step["request"])
                    job = response["job"]
                    step["jobId"] = job["id"]
                    self.save(run)
                if job["status"] == "completed":
                    run["index"] += 1
                    self.save(run)
                    continue
                if job["status"] not in ("queued", "running"):
                    run.update(status="paused", message="当前镜头失败或结果未知，请重试该镜头")
                    self.save(run)
                    return
                run["message"] = f"正在生成第 {run['index'] + 1} / {len(run['steps'])} 镜"
                self.save(run)
                await asyncio.sleep(1)
            if run["status"] == "running":
                run.update(status="completed", message="全部镜头已生成")
                self.save(run)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if run["status"] == "running":
                run.update(
                    status="paused",
                    message=str(exc) or "提交中断或镜头已变化，请检查后继续；未自动重试",
                )
                self.save(run)

    def get(self, ident):
        if ident not in self.runs:
            raise HTTPException(404, "执行记录不存在")
        result = deepcopy(self.runs[ident])
        # 当前尝试与历史尝试的任务都返回：旧结果保留为可查看记录
        job_ids = []
        for step in result["steps"]:
            if step.get("jobId"):
                job_ids.append(step["jobId"])
            for attempt in step.get("attempts", []) or []:
                if attempt.get("jobId"):
                    job_ids.append(attempt["jobId"])
        result["jobs"] = [deepcopy(self.jobs.jobs[i]) for i in job_ids if i in self.jobs.jobs]
        return result

    def retry(self, ident):
        """失败镜头重试：创建新的尝试标识并保留旧结果；unknown 只对账不重提。"""
        run = self.runs.get(ident)
        if not run:
            raise HTTPException(404, "执行记录不存在")
        if run["status"] in ("completed", "cancelled"):
            raise HTTPException(409, "执行已结束，不能重试镜头")
        if run["status"] == "running":
            raise HTTPException(409, "执行仍在进行中，请先暂停")
        if run["index"] >= len(run["steps"]):
            raise HTTPException(409, "没有可重试的镜头")
        task = self.tasks.get(ident)
        if task and not task.done():
            raise HTTPException(409, "当前准备正在停止，请稍后重试")
        step = run["steps"][run["index"]]
        job = self.jobs.jobs.get(step.get("jobId"))
        status = job.get("status") if job else None
        if status == "unknown":
            resumed = self.jobs.resume(step["jobId"]) if step.get("jobId") else None
            if not resumed:
                raise HTTPException(
                    409, "该任务没有可查询的上游结果，请取消执行后重新开始"
                )
            run.update(status="running", message="正在对账上游任务，未重新调用模型")
            self.save(run)
            self.launch(run)
            return self.get(ident)
        if status not in (None, "failed", "cancelled"):
            raise HTTPException(409, "当前镜头仍在生成中，不能重试")
        self.check_shot(self.documents.get(run["documentId"]), step)
        attempts = step.setdefault("attempts", [])
        if step.get("jobId") or step["request"].get("requestId"):
            attempts.append(
                {
                    "requestId": step["request"].get("requestId"),
                    "jobId": step.get("jobId"),
                    "status": status,
                }
            )
        step["request"] = {**step["request"], "requestId": str(uuid4())}
        step["jobId"] = None
        run.update(status="running", message="重试当前镜头；旧尝试与结果已保留")
        self.save(run)
        self.launch(run)
        return self.get(ident)

    def cancel_owned(self, run):
        """取消只处理本轮步骤归属的任务；用户单独提交的任务不受影响。"""
        for step in run["steps"][run["index"] :]:
            job = self.jobs.jobs.get(step.get("jobId")) if step.get("jobId") else None
            if job is None:
                job = self.find_request(run["documentId"], step.get("request", {}).get("requestId"))
            if job and job.get("status") in ("queued", "running", "unknown"):
                self.jobs.cancel(job["id"])

    async def control(self, ident, action):
        run = self.runs.get(ident)
        if not run:
            raise HTTPException(404, "执行记录不存在")
        if action == "retry":
            return self.retry(ident)
        if action not in ("pause", "resume", "cancel"):
            raise ValueError("未知执行操作")
        if run["status"] in ("completed", "cancelled"):
            return self.get(ident)
        task = self.tasks.get(ident)
        if action == "resume":
            if run["status"] == "running":
                return self.get(ident)
            if task and not task.done():
                raise HTTPException(409, "当前准备正在停止，请稍后继续")
            run.update(status="running", message="继续执行")
            self.save(run)
            self.launch(run)
        else:
            run.update(
                status="paused" if action == "pause" else "cancelled",
                message="已暂停，已提交镜头可继续完成" if action == "pause" else "已取消后续执行",
            )
            self.save(run)
            if action == "cancel":
                if task and not task.done():
                    task.cancel()
                    # 等待停止后再清理归属任务，覆盖提交中创建、尚未记录 jobId 的请求。
                    await asyncio.wait([task], timeout=5)
                self.cancel_owned(run)
            elif task:
                task.cancel()
        return self.get(ident)

    async def close(self):
        idents = list(self.tasks)
        await asyncio.gather(
            *(self.control(ident, "pause") for ident in idents), return_exceptions=True
        )
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
