"""Durable planning results; a lost HTTP response never requires another model call."""

import asyncio
import hashlib
import json
from copy import deepcopy
from uuid import UUID

from fastapi import HTTPException

from .common import now, read_json, write_json
from .storyboard import PLANNING_WORKFLOWS, plan_storyboard


class StoryboardJobs:
    def __init__(self, data, planner=plan_storyboard):
        self.root = data / "storyboard-jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.planner = planner
        self.jobs = {}
        self.tasks = {}
        self.slot = asyncio.Semaphore(1)
        for path in self.root.glob("*.json"):
            job = read_json(path, None)
            if not isinstance(job, dict) or job.get("id") != path.stem:
                continue
            try:
                UUID(path.stem)
            except ValueError:
                continue
            if job.get("status") in ("queued", "running"):
                job.update(status="failed", error="服务重启中断了规划，请重新规划；未自动重试")
                self.save(job)
            self.jobs[path.stem] = job

    def save(self, job):
        job["updatedAt"] = now()
        write_json(self.root / f"{job['id']}.json", job)

    def submit(self, ident, doc_id, prompt, workflow, config, client):
        try:
            if str(UUID(ident)) != ident:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("规划请求标识无效") from None
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            raise ValueError("请提供不超过 12000 字的创作需求或剧本")
        if not isinstance(workflow, str) or workflow not in PLANNING_WORKFLOWS:
            raise ValueError("不支持的创作工作流")
        fingerprint = hashlib.sha256(json.dumps([doc_id, prompt, workflow]).encode()).hexdigest()
        if ident in self.jobs:
            job = self.jobs[ident]
            if job["fingerprint"] != fingerprint:
                raise HTTPException(409, "相同规划标识不能用于不同创作需求")
            return deepcopy(job)
        if sum(job["status"] in ("queued", "running") for job in self.jobs.values()) >= 5:
            raise HTTPException(429, "规划队列已满，请稍后重试")
        job = dict(
            id=ident,
            documentId=doc_id,
            prompt=prompt,
            workflow=workflow,
            fingerprint=fingerprint,
            status="queued",
            createdAt=now(),
            error=None,
        )
        self.save(job)
        self.jobs[ident] = job
        task = asyncio.create_task(self.run(job, deepcopy(config), client))
        self.tasks[ident] = task
        task.add_done_callback(lambda _task: self.tasks.pop(ident, None))
        return deepcopy(job)

    async def run(self, job, config, client):
        try:
            async with self.slot:
                if job["status"] != "queued":
                    return
                job["status"] = "running"
                self.save(job)
                result = await self.planner(job["prompt"], config, client, job["workflow"])
                if job["status"] == "running":
                    job.update(status="completed", storyboard=result)
                    self.save(job)
        except asyncio.CancelledError:
            if job["status"] in ("queued", "running"):
                job.update(status="cancelled", error="规划已取消")
                self.save(job)
            raise
        except Exception:
            if job["status"] == "running":
                job.update(
                    status="failed", error="分镜规划失败，请检查文本模型配置及创作需求；未自动重试"
                )
                self.save(job)

    def list_for_document(self, doc_id):
        jobs = sorted(
            (job for job in self.jobs.values() if job["documentId"] == doc_id),
            key=lambda job: (job["createdAt"], job["id"]),
            reverse=True,
        )
        return [
            {
                key: job.get(key)
                for key in ("id", "documentId", "prompt", "workflow", "status", "createdAt")
            }
            for job in jobs[:20]
        ]

    def get(self, ident):
        job = self.jobs.get(ident)
        if job is None:
            raise HTTPException(404, "规划任务不存在")
        return deepcopy(job)

    def cancel(self, ident):
        job = self.get(ident)
        if job["status"] in ("queued", "running"):
            stored = self.jobs[ident]
            stored.update(status="cancelled", error="规划已取消")
            self.save(stored)
            if task := self.tasks.get(ident):
                task.cancel()
        return self.get(ident)

    async def close(self):
        tasks = list(self.tasks.values())
        for ident, task in list(self.tasks.items()):
            job = self.jobs[ident]
            if job["status"] in ("queued", "running"):
                job.update(status="failed", error="服务关闭中断了规划，请重新规划；未自动重试")
                self.save(job)
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
