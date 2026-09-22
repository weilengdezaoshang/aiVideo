import asyncio
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from backend.common import write_json
from backend.storyboard_runs import StoryboardRuns


def fixture():
    steps = []
    doc = dict(objects={}, storyboard=dict(shots=[]))
    for index in range(2):
        node = f"node-{index}"
        draft = dict(prompt=f"镜头{index}", model="mock", durationSec=5)
        doc["objects"][node] = dict(kind="image", nodeDraft=draft)
        doc["storyboard"]["shots"].append(dict(id=node, nodeId=node, locked=False))
        steps.append(
            dict(
                shotId=node,
                draft=draft,
                request=dict(
                    documentId="doc", clientRef=node, requestId=str(uuid4()), kind="image", **draft
                ),
            )
        )
    return doc, steps


def submit_recorder(jobs, submissions):
    async def submit(raw):
        submissions.append((raw["clientRef"], raw["requestId"]))
        job = dict(
            id=raw["requestId"], status="running", documentId="doc", requestId=raw["requestId"]
        )
        jobs.jobs[job["id"]] = job
        return dict(job=job)

    return submit


def test_sequential_pause_resume_cancel_and_recovery(tmp_path):
    async def run():
        doc, steps = fixture()
        cancelled, submitted = [], []
        jobs = SimpleNamespace(jobs={}, cancel=lambda ident: cancelled.append(ident))

        async def submit(raw):
            submitted.append(raw["requestId"])
            job = dict(
                id=raw["requestId"], status="running", documentId="doc", requestId=raw["requestId"]
            )
            jobs.jobs[job["id"]] = job
            return dict(job=job)

        documents = SimpleNamespace(get=lambda ident: doc if ident == "doc" else None)
        runs = StoryboardRuns(tmp_path, documents, jobs, submit)
        ident = str(uuid4())
        runs.start(ident, "doc", steps)
        await asyncio.sleep(0)
        assert len(submitted) == 1
        assert runs.start(ident, "doc", steps)["status"] == "running"
        with pytest.raises(HTTPException):
            runs.start(str(uuid4()), "doc", steps)
        await runs.control(ident, "pause")
        await asyncio.gather(*list(runs.tasks.values()), return_exceptions=True)
        assert cancelled == []
        jobs.jobs[submitted[0]]["status"] = "completed"
        await runs.control(ident, "resume")
        await asyncio.sleep(0)
        assert len(submitted) == 2
        assert runs.get(ident)["index"] == 1
        await runs.control(ident, "cancel")
        await asyncio.gather(*list(runs.tasks.values()), return_exceptions=True)
        assert cancelled == [submitted[1]]
        restored = StoryboardRuns(tmp_path, documents, jobs, submit)
        assert restored.get(ident)["status"] == "cancelled"
        assert (await restored.control(ident, "resume"))["status"] == "cancelled"
        assert len(restored.get(ident)["jobs"]) == 2
        await runs.close()
        await restored.close()

    asyncio.run(run())


def test_changed_shot_and_unknown_job_pause_without_resubmitting(tmp_path):
    async def run():
        doc, steps = fixture()
        jobs = SimpleNamespace(jobs={}, cancel=lambda ident: None)
        calls = []

        async def submit(raw):
            calls.append(raw)
            job = dict(id="unknown", status="unknown")
            jobs.jobs["unknown"] = job
            return dict(job=job)

        runs = StoryboardRuns(tmp_path, SimpleNamespace(get=lambda _: doc), jobs, submit)
        ident = str(uuid4())
        runs.start(ident, "doc", steps)
        await asyncio.gather(*list(runs.tasks.values()))
        assert runs.get(ident)["status"] == "paused"
        await runs.control(ident, "resume")
        await asyncio.gather(*list(runs.tasks.values()))
        assert len(calls) == 1
        assert runs.get(ident)["status"] == "paused"
        await runs.control(ident, "cancel")
        changed = deepcopy(steps)
        changed[0]["request"]["prompt"] = "偷偷更改"
        with pytest.raises(ValueError, match="不一致"):
            runs.start(str(uuid4()), "doc", changed)
        await runs.close()

    asyncio.run(run())


def test_api_executes_mock_media_without_browser(client):
    import time

    doc, steps = fixture()
    created = client.post("/api/documents", json={"name": "服务端分镜"}).json()["document"]
    doc_id = created["id"]
    for step in steps:
        step["draft"]["model"] = "mock-diffusion-xl"
        step["request"].update(
            documentId=doc_id, model="mock-diffusion-xl", width=64, height=64, steps=1, batchCount=1
        )
    for shot in doc["storyboard"]["shots"]:
        shot.update(title="镜头", visual="画面", dialogue="", camera="", durationFrames=150)
    doc["storyboard"].update(version=1, outline="测试")
    client.app.state.documents.save(doc_id, dict(name="服务端分镜", order=list(doc["objects"]), **doc))
    ident = str(uuid4())
    response = client.post(
        "/api/storyboards/runs", json=dict(id=ident, documentId=doc_id, steps=steps)
    )
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        run = client.get(f"/api/storyboards/runs/{ident}").json()["run"]
        if run["status"] != "running":
            break
        time.sleep(0.05)
    assert run["status"] == "completed", run
    assert run["index"] == 2
    assert len(run["jobs"]) == 2
    assert all(job["status"] == "completed" and job["images"] for job in run["jobs"])
    listed = client.get(f"/api/storyboards/runs?documentId={doc_id}").json()["runs"]
    assert [item["id"] for item in listed] == [ident]
    assert listed[0]["status"] == "completed"
    assert listed[0]["total"] == 2


def test_retry_failed_shot_creates_new_attempt_and_keeps_records(tmp_path):
    async def run():
        doc, steps = fixture()
        jobs = SimpleNamespace(jobs={}, cancel=lambda ident: None, resume=lambda ident: None)
        submissions = []
        runs = StoryboardRuns(
            tmp_path, SimpleNamespace(get=lambda _: doc), jobs, submit_recorder(jobs, submissions)
        )
        ident = str(uuid4())
        runs.start(ident, "doc", steps)
        await asyncio.sleep(0)
        first = submissions[0][1]
        await runs.control(ident, "pause")
        await asyncio.gather(*list(runs.tasks.values()), return_exceptions=True)
        jobs.jobs[first]["status"] = "completed"
        await runs.control(ident, "resume")
        await asyncio.sleep(0)
        second = submissions[1][1]
        assert (submissions[0][0], submissions[1][0]) == ("node-0", "node-1")
        # 执行器仍在轮询时 retry 被拒绝，必须先暂停
        jobs.jobs[second]["status"] = "failed"
        with pytest.raises(HTTPException):
            await runs.control(ident, "retry")
        await runs.control(ident, "pause")
        await asyncio.gather(*list(runs.tasks.values()), return_exceptions=True)
        # 恢复后执行器发现失败镜头并暂停，等待用户重试
        await runs.control(ident, "resume")
        await asyncio.sleep(0)
        assert runs.get(ident)["status"] == "paused"
        await runs.control(ident, "pause")
        await asyncio.gather(*list(runs.tasks.values()), return_exceptions=True)
        result = await runs.control(ident, "retry")
        assert result["status"] == "running"
        step = runs.runs[ident]["steps"][1]
        assert step["request"]["requestId"] != second
        assert step["attempts"][0]["requestId"] == second
        assert step["attempts"][0]["status"] == "failed"
        await asyncio.sleep(0)
        # 只有第二镜重试；第一镜不重复提交，旧尝试仍在记录中
        assert len(submissions) == 3
        assert submissions[2][0] == "node-1"
        assert submissions[2][1] != second
        assert len(runs.get(ident)["jobs"]) == 3
        jobs.jobs[submissions[2][1]]["status"] = "completed"
        await runs.control(ident, "pause")
        await asyncio.gather(*list(runs.tasks.values()), return_exceptions=True)
        await runs.control(ident, "resume")
        await asyncio.sleep(0)
        assert runs.get(ident)["status"] == "completed"
        await runs.close()

    asyncio.run(run())


def test_retry_unknown_reconciles_upstream_without_model_call(tmp_path):
    async def run():
        doc, steps = fixture()
        jobs = SimpleNamespace(jobs={}, cancel=lambda ident: None)
        submissions = []
        resumes = []

        async def submit(raw):
            submissions.append(raw["requestId"])
            job = dict(
                id=raw["requestId"],
                status="unknown",
                documentId="doc",
                requestId=raw["requestId"],
                externalTaskId="upstream-1",
            )
            jobs.jobs[job["id"]] = job
            return dict(job=job)

        def resume(ident):
            resumes.append(ident)
            jobs.jobs[ident].update(status="completed", images=[dict(id="img", url="/images/x.png")])
            return jobs.jobs[ident]

        jobs.resume = resume
        runs = StoryboardRuns(tmp_path, SimpleNamespace(get=lambda _: doc), jobs, submit)
        ident = str(uuid4())
        runs.start(ident, "doc", steps[:1])
        await asyncio.sleep(0)
        await runs.control(ident, "pause")
        await asyncio.gather(*list(runs.tasks.values()), return_exceptions=True)
        job_id = runs.runs[ident]["steps"][0]["jobId"]
        result = await runs.control(ident, "retry")
        assert resumes == [job_id]
        assert result["steps"][0]["jobId"] == job_id
        await asyncio.sleep(0)
        assert runs.get(ident)["status"] == "completed"
        assert len(submissions) == 1, "unknown 必须先对账，不得重新调用模型"
        assert jobs.jobs[job_id]["externalTaskId"] == "upstream-1"
        await runs.close()

    asyncio.run(run())


def test_cancel_only_cancels_run_owned_tasks(tmp_path):
    async def run():
        doc, steps = fixture()
        cancelled = []
        jobs = SimpleNamespace(jobs={}, cancel=lambda ident: cancelled.append(ident))
        runs = StoryboardRuns(
            tmp_path, SimpleNamespace(get=lambda _: doc), jobs, submit_recorder(jobs, [])
        )
        # 用户单独提交的任务：请求不属于任何执行步骤
        jobs.jobs["user-task"] = dict(
            id="user-task", status="running", documentId="doc", requestId="user-request"
        )
        ident = str(uuid4())
        runs.start(ident, "doc", steps)
        await asyncio.sleep(0)
        await runs.control(ident, "cancel")
        owned = {step["jobId"] for step in runs.runs[ident]["steps"] if step.get("jobId")}
        assert sorted(cancelled) == sorted(owned)
        assert "user-task" not in cancelled
        await runs.close()

    asyncio.run(run())


def test_cancel_covers_submitted_but_unrecorded_request(tmp_path):
    async def run():
        doc, steps = fixture()
        cancelled = []
        jobs = SimpleNamespace(jobs={}, cancel=lambda ident: cancelled.append(ident))
        runs = StoryboardRuns(
            tmp_path, SimpleNamespace(get=lambda _: doc), jobs, submit_recorder(jobs, [])
        )
        ident = str(uuid4())
        runs.start(ident, "doc", steps)
        await asyncio.sleep(0)
        await runs.control(ident, "pause")
        await asyncio.gather(*list(runs.tasks.values()), return_exceptions=True)
        step = runs.runs[ident]["steps"][0]
        recorded = step["jobId"]
        # 模拟取消与提交竞争：任务已创建但执行循环未记录 jobId
        step["jobId"] = None
        await runs.control(ident, "cancel")
        assert cancelled == [recorded]
        await runs.close()

    asyncio.run(run())


def test_check_shot_reference_and_busy_node_guard(tmp_path):
    async def submit(raw):
        raise AssertionError("校验失败时不应提交")

    async def run():
        doc, steps = fixture()
        # 参考图一致性：请求中的参考资产必须与草稿一致
        steps[0]["draft"]["references"] = [dict(assetId="asset-1", ext="png", name="参考")]
        steps[0]["request"]["referenceAssetIds"] = ["asset-other"]
        jobs = SimpleNamespace(jobs={}, cancel=lambda ident: None, resume=lambda ident: None)
        runs = StoryboardRuns(tmp_path, SimpleNamespace(get=lambda _: doc), jobs, submit)
        with pytest.raises(ValueError, match="参考图"):
            runs.start(str(uuid4()), "doc", steps)
        steps[0]["request"]["referenceAssetIds"] = ["asset-1"]
        # 节点已有进行中的其他任务：拒绝启动，防止同一节点双份生成
        node_id = steps[0]["request"]["clientRef"]
        doc["objects"][node_id]["nodeRun"] = dict(requestId="other-request", status="running")
        with pytest.raises(ValueError, match="已有进行中的生成任务"):
            runs.start(str(uuid4()), "doc", steps)
        await runs.close()

    asyncio.run(run())


def test_list_runs_for_document(tmp_path):
    async def run():
        doc, steps = fixture()
        jobs = SimpleNamespace(jobs={}, cancel=lambda ident: None, resume=lambda ident: None)
        runs = StoryboardRuns(
            tmp_path, SimpleNamespace(get=lambda _: doc), jobs, submit_recorder(jobs, [])
        )
        assert runs.list_for_document("doc") == []
        ident = str(uuid4())
        runs.start(ident, "doc", steps)
        await asyncio.sleep(0)
        listed = runs.list_for_document("doc")
        assert [item["id"] for item in listed] == [ident]
        assert listed[0]["status"] == "running"
        assert listed[0]["total"] == 2
        assert runs.list_for_document("other") == []
        await runs.control(ident, "cancel")
        assert runs.list_for_document("doc")[0]["status"] == "cancelled"
        await runs.close()

    asyncio.run(run())


def test_restart_marks_running_run_paused(tmp_path):
    doc, steps = fixture()
    ident = str(uuid4())
    (tmp_path / "storyboard-runs").mkdir()
    write_json(
        tmp_path / "storyboard-runs" / f"{ident}.json",
        dict(
            id=ident,
            documentId="doc",
            steps=deepcopy(steps),
            index=1,
            status="running",
            message="正在生成第 2 / 2 镜",
            fingerprint="x",
            createdAt="2026-09-14T00:00:00",
        ),
    )

    async def submit(raw):
        raise AssertionError("重启恢复后不得自动重提")

    async def main():
        jobs = SimpleNamespace(jobs={}, cancel=lambda ident: None, resume=lambda ident: None)
        runs = StoryboardRuns(tmp_path, SimpleNamespace(get=lambda _: doc), jobs, submit)
        restored = runs.get(ident)
        assert restored["status"] == "paused"
        assert "重启" in restored["message"]
        await runs.close()

    asyncio.run(main())
