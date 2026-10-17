"""人工审核与裁决(技术方案 §11,验收 REVIEW-01/02)。

- 审核任务绑定具体 trial/检查项/素材/规则版本;领取用租约 + 乐观锁。
- task.json 等状态文件原子写入;状态迁移在同一把进程锁内完成,并发更新互斥。
- 意见追加保存:修改生成 supersedes 链,不覆盖任何已提交意见(§11.4)。
- 裁决绑定一组确定意见;不修改自动评分,报告分列自动/人工(P3 呈现)。
- 抽样策略(必审/风险/随机)版本化,run 完成后由调度器按策略生成任务,
  抽样依据写入任务记录(§11.2)。
- 线上问题转用例:审核结论 → case draft → 冻结进数据集(§9.4)。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..common import atomic_write_text, now
from .models import CaseVersion, RunState

# 任务状态机:pending -> claimed -> submitted -> disputed -> adjudicated。
_REVIEW_LOCKS: dict[str, threading.RLock] = {}
_REVIEW_LOCKS_GUARD = threading.Lock()


# 任务状态机(§11.2)的唯一事实源:状态 → 允许迁入的状态集合。
# 已裁决允许再次裁决(追加式,§11.4);提交允许修订(追加新意见)。
_REVIEW_TRANSITIONS: dict[str, frozenset] = {
    "pending": frozenset({"claimed"}),
    "claimed": frozenset({"claimed", "submitted"}),
    "submitted": frozenset({"submitted", "disputed", "adjudicated"}),
    "disputed": frozenset({"adjudicated"}),
    "adjudicated": frozenset({"adjudicated"}),
}


def _require_transition(task_id: str, current: str, target: str) -> None:
    if target not in _REVIEW_TRANSITIONS.get(current, frozenset()):
        raise ReviewConflict(f"任务状态为 {current},不能迁移到 {target}")


def _task_lock(task_id: str) -> threading.RLock:
    """按任务 ID 共享的进程锁:领取/提交/裁决的状态迁移互斥(并发更新原子化)。"""
    with _REVIEW_LOCKS_GUARD:
        if task_id not in _REVIEW_LOCKS:
            _REVIEW_LOCKS[task_id] = threading.RLock()
        return _REVIEW_LOCKS[task_id]


class ReviewConflict(ValueError):
    """code=REVIEW_CONFLICT(§14.4):租约失效或 revision 过期,对应 HTTP 409。"""

    code = "REVIEW_CONFLICT"


class ReviewVerdict(BaseModel):
    checkId: str
    verdict: str  # pass / fail / unsure / insufficient_evidence
    note: str | None = None
    region: str | None = None
    frameTimeMs: int | None = None


class ReviewTask(BaseModel):
    schemaVersion: int = 2
    reviewTaskId: str
    runId: str
    trialId: str
    caseId: str
    artifactIds: list[str] = Field(default_factory=list)
    gradeRefs: list[dict] = Field(default_factory=list)  # gradeId + evaluator 版本
    rubricVersion: str = "1"
    priority: str = "normal"  # required / risk / random(§11.2 三类抽样)
    status: str = "pending"  # pending -> claimed -> submitted -> disputed -> adjudicated
    revision: int = 1
    claim: dict | None = None  # {reviewer, leaseUntil}
    # 抽样依据:自动创建时记录规则与策略版本,人工创建记 manual(§11.2)。
    sampling: dict = Field(default_factory=lambda: {"rule": "manual", "policyVersion": None})
    createdAt: str = Field(default_factory=now)


class Review(BaseModel):
    opinionId: str
    reviewTaskId: str
    revision: int
    reviewer: str
    verdicts: list[ReviewVerdict]
    agreeWithAuto: bool | None = None
    category: str | None = None  # 问题分类(数量/文字/一致性...)
    note: str | None = None
    uncertain: bool = False
    supersededBy: str | None = None
    submittedAt: str = Field(default_factory=now)


class Adjudication(BaseModel):
    adjudicationId: str
    reviewTaskId: str
    boundOpinionIds: list[str]
    finalVerdict: str  # pass / fail / undetermined
    reason: str
    decidedBy: str
    rubricVersion: str = "1"  # 裁决绑定的规则版本(§11.4)
    artifactIds: list[str] = Field(default_factory=list)  # 裁决依据的素材
    at: str = Field(default_factory=now)


class ReviewStore:
    """reviews/<reviewTaskId>/:task.json + opinions.jsonl + adjudications.jsonl(全部追加)。"""

    def __init__(self, store: Path):
        self.root = store / "reviews"

    def _dir(self, task_id: str) -> Path:
        return self.root / task_id

    def create(
        self,
        run_id: str,
        trial_id: str,
        case_id: str,
        artifact_ids=None,
        grade_refs=None,
        rubric_version="1",
        priority="normal",
        sampling: dict | None = None,
    ) -> ReviewTask:
        with _task_lock(f"create:{self.root}:{run_id}:{trial_id}"):
            for existing in self.list():
                if existing.runId == run_id and existing.trialId == trial_id:
                    return existing  # 同一 trial 不重复建任务
            task = ReviewTask(
                reviewTaskId=f"review-{uuid.uuid4().hex[:12]}",
                runId=run_id,
                trialId=trial_id,
                caseId=case_id,
                artifactIds=artifact_ids or [],
                gradeRefs=grade_refs or [],
                rubricVersion=rubric_version,
                priority=priority,
                sampling=sampling or {"rule": "manual", "policyVersion": None},
            )
            directory = self._dir(task.reviewTaskId)
            directory.mkdir(parents=True)
            atomic_write_text(
                directory / "task.json",
                json.dumps(task.model_dump(), ensure_ascii=False, indent=2),
            )
            return task

    def get(self, task_id: str) -> ReviewTask:
        task_file = self._dir(task_id) / "task.json"
        if not task_file.is_file():
            raise FileNotFoundError(f"审核任务不存在:{task_id}")
        return ReviewTask.model_validate(json.loads(task_file.read_text(encoding="utf-8")))

    def _save(self, task: ReviewTask) -> None:
        atomic_write_text(
            self._dir(task.reviewTaskId) / "task.json",
            json.dumps(task.model_dump(), ensure_ascii=False, indent=2),
        )

    def list(self, status: str | None = None) -> list[ReviewTask]:
        if not self.root.is_dir():
            return []
        tasks = []
        for directory in sorted(self.root.iterdir()):
            task_file = directory / "task.json"
            if task_file.is_file():
                task = ReviewTask.model_validate(json.loads(task_file.read_text(encoding="utf-8")))
                if status is None or task.status == status:
                    tasks.append(task)
        return tasks

    # ---------- 领取(租约 + 乐观锁,REVIEW-01) ----------

    def claim(
        self, task_id: str, reviewer: str, expected_revision: int, lease_seconds: int = 3600
    ) -> ReviewTask:
        if not reviewer.strip():
            raise ValueError("领取必须提供审核人身份")
        from datetime import UTC, datetime, timedelta

        with _task_lock(task_id):
            task = self.get(task_id)
            _require_transition(task_id, task.status, "claimed")
            if task.revision != expected_revision:
                raise ReviewConflict(f"revision 过期:当前 {task.revision},请求基于 {expected_revision}")
            if task.status == "claimed" and task.claim:
                lease_until = datetime.fromisoformat(task.claim["leaseUntil"].replace("Z", "+00:00"))
                if datetime.now(UTC) < lease_until and task.claim.get("reviewer") != reviewer:
                    raise ReviewConflict(
                        f"任务正被 {task.claim.get('reviewer')} 租用,租约至 {task.claim['leaseUntil']}"
                    )
            task.status = "claimed"
            task.revision += 1
            task.claim = {
                "reviewer": reviewer,
                "leaseUntil": (
                    datetime.now(UTC) + timedelta(seconds=lease_seconds)
                ).isoformat().replace("+00:00", "Z"),
            }
            self._save(task)
            return task

    # ---------- 独立意见(追加式) ----------

    def _opinions(self, task_id: str) -> list[Review]:
        opinions_file = self._dir(task_id) / "opinions.jsonl"
        if not opinions_file.is_file():
            return []
        # 追加式日志:同一意见的取代标记后写覆盖先写;原文历史仍在文件中可审计。
        latest: dict[str, Review] = {}
        for line in opinions_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict) and item.get("opinionId"):
                latest[item["opinionId"]] = Review.model_validate(item)
        return list(latest.values())

    def submit_opinion(
        self,
        task_id: str,
        reviewer: str,
        verdicts: list[dict],
        expected_revision: int,
        agree_with_auto: bool | None = None,
        category: str | None = None,
        note: str | None = None,
        uncertain: bool = False,
    ) -> Review:
        if not reviewer.strip():
            raise ValueError("提交意见必须提供审核人身份")
        with _task_lock(task_id):
            task = self.get(task_id)
            if task.revision != expected_revision:
                raise ReviewConflict(f"revision 过期:当前 {task.revision},请求基于 {expected_revision}")
            _require_transition(task_id, task.status, "submitted")
            # 身份关联:领取人才能提交意见,防止他人借租约提交(REVIEW-01)。
            if task.claim and task.claim.get("reviewer") != reviewer:
                raise ReviewConflict(
                    f"任务由 {task.claim.get('reviewer')} 领取,{reviewer} 不能提交意见"
                )
            opinion = Review(
                opinionId=f"opinion-{uuid.uuid4().hex[:12]}",
                reviewTaskId=task_id,
                revision=expected_revision,
                reviewer=reviewer,
                verdicts=[ReviewVerdict.model_validate(item) for item in verdicts],
                agreeWithAuto=agree_with_auto,
                category=category,
                note=note,
                uncertain=uncertain,
            )
            with (self._dir(task_id) / "opinions.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(opinion.model_dump(), ensure_ascii=False) + "\n")
            task.status = "submitted"
            task.revision += 1
            self._save(task)
            return opinion

    def supersede(self, task_id: str, opinion_id: str, reviewer: str, verdicts: list[dict]) -> Review:
        """修订 = 新意见追加并标记旧意见被取代;旧意见原文保持可读(REVIEW-02)。"""
        opinions = self._opinions(task_id)
        target = next((item for item in opinions if item.opinionId == opinion_id), None)
        if target is None:
            raise FileNotFoundError(f"意见不存在:{opinion_id}")
        if target.supersededBy:
            raise ReviewConflict("该意见已被取代,不能再次修订")
        replacement = self.submit_opinion(
            task_id,
            reviewer,
            verdicts,
            expected_revision=self.get(task_id).revision,
            note=f"取代 {opinion_id}",
        )
        target.supersededBy = replacement.opinionId
        with (self._dir(task_id) / "opinions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(target.model_dump(), ensure_ascii=False) + "\n")  # 追加取代标记,原文保留
        return replacement

    def dispute(self, task_id: str, by: str, reason: str) -> ReviewTask:
        with _task_lock(task_id):
            task = self.get(task_id)
            _require_transition(task_id, task.status, "disputed")
            task.status = "disputed"
            task.revision += 1
            self._save(task)
            with (self._dir(task_id) / "opinions.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {"kind": "dispute", "by": by, "reason": reason, "at": now()}, ensure_ascii=False
                    )
                    + "\n"
                )
            return task

    # ---------- 裁决 ----------

    def adjudicate(
        self, task_id: str, bound_opinion_ids: list[str], final_verdict: str, reason: str, by: str
    ) -> Adjudication:
        if final_verdict not in {"pass", "fail", "undetermined"}:
            raise ValueError(f"未知最终裁决:{final_verdict}")
        if not by.strip():
            raise ValueError("裁决必须提供裁决人身份")
        with _task_lock(task_id):
            task = self.get(task_id)
            _require_transition(task_id, task.status, "adjudicated")
            # 已裁决任务允许再次裁决:产生新记录(append-only),旧裁决不被改写(§11.4)。
            known = {item.opinionId for item in self._opinions(task_id)}
            unknown = [oid for oid in bound_opinion_ids if oid not in known]
            if unknown:
                raise ValueError(f"裁决绑定了不存在的意见:{unknown}")
            adjudication = Adjudication(
                adjudicationId=f"adjudication-{uuid.uuid4().hex[:12]}",
                reviewTaskId=task_id,
                boundOpinionIds=list(bound_opinion_ids),
                finalVerdict=final_verdict,
                reason=reason,
                decidedBy=by,
                rubricVersion=task.rubricVersion,
                artifactIds=list(task.artifactIds),
            )
            with (self._dir(task_id) / "adjudications.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(adjudication.model_dump(), ensure_ascii=False) + "\n")
            task.status = "adjudicated"
            task.revision += 1
            self._save(task)
            return adjudication

    def adjudications(self, task_id: str) -> list[Adjudication]:
        adjudications_file = self._dir(task_id) / "adjudications.jsonl"
        if not adjudications_file.is_file():
            return []
        return [
            Adjudication.model_validate(json.loads(line))
            for line in adjudications_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def latest_adjudication(self, task_id: str) -> Adjudication | None:
        records = self.adjudications(task_id)
        return records[-1] if records else None

    def task_detail(self, task_id: str) -> dict:
        """任务 + 追加式意见/争议/裁决全历史(审核页面与门禁水位共用)。"""
        task = self.get(task_id)
        dispute_marks = []
        opinions_file = self._dir(task_id) / "opinions.jsonl"
        if opinions_file.is_file():
            for line in opinions_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if item.get("kind") == "dispute":
                    dispute_marks.append(item)
        return {
            "task": task.model_dump(),
            "opinions": [item.model_dump() for item in self._opinions(task_id)],
            "disputes": dispute_marks,
            "adjudications": [item.model_dump() for item in self.adjudications(task_id)],
        }

    # ---------- 问题转用例(§9.4) ----------

    def create_case_draft(self, task_id: str, case: dict) -> Path:
        """审核确认的问题固化为用例草稿;校验交给 CaseVersion 与发布冻结。"""
        task = self.get(task_id)
        if task.status not in {"submitted", "disputed", "adjudicated"}:
            raise ReviewConflict(f"任务状态为 {task.status},问题尚未确认")
        drafts = self.root / "case-drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        draft_id = f"draft-{uuid.uuid4().hex[:12]}"
        payload = {
            "draftId": draft_id,
            "fromReviewTask": task_id,
            "runId": task.runId,
            "trialId": task.trialId,
            "case": case,
            "createdAt": now(),
        }
        path = drafts / f"{draft_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def publish_case_draft(store: Path, draft_id: str, dataset_id: str, split: str = "regression") -> dict:
    """草稿 → 数据集新版本(regression 集);校验失败按 DATA-02 拒绝。"""
    from .datasets import DatasetStore, validate_source_params

    draft_file = store / "reviews" / "case-drafts" / f"{draft_id}.json"
    if not draft_file.is_file():
        raise FileNotFoundError(f"草稿不存在:{draft_id}")
    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    case = CaseVersion.model_validate(draft["case"])
    problems = validate_source_params([case])
    if problems:
        raise ValueError("草稿用例预检失败:" + ";".join(problems))
    manifest = DatasetStore(store).freeze([case], dataset_id, split=split, source_path=draft_file)
    return manifest.model_dump()


# ---------- 抽样策略(§11.2):run 完成后自动生成审核任务 ----------

SAMPLING_SCHEMA_VERSION = 2


class ReviewSamplingPolicy(BaseModel):
    """必审/风险抽样/随机抽样的版本化策略;抽样依据写入每个任务。"""

    schemaVersion: int = SAMPLING_SCHEMA_VERSION
    enabled: bool = True
    required: Literal["fail_or_undetermined", "none", "all"] = "fail_or_undetermined"  # 兼容旧配置；默认只送不可靠/标记题，不再把普通 FAIL 必送
    riskRate: float = Field(default=1.0, ge=0, le=1)
    randomRate: float = Field(default=0.05, ge=0, le=1)
    riskThreshold: float = Field(default=0.8, ge=0, le=1)  # 模糊带参考阈值
    riskMargin: float = Field(default=0.05, ge=0, le=1)
    grayMin: float = Field(default=0.4, ge=0, le=1)
    grayMax: float = Field(default=0.7, ge=0, le=1)
    gateScoreThreshold: float | None = Field(default=None, ge=0, le=1)  # 配置质量分门槛时检测冲突
    reviewAgentFlowFails: bool = False
    agentFailures: bool = False  # 兼容旧显式开关
    includeSynthetic: bool = False  # mock/stub 管线自测默认排除
    seed: int = 0

    @model_validator(mode="after")
    def ordered_gray_range(self):
        if self.grayMin > self.grayMax:
            raise ValueError("灰区下限不得大于上限")
        return self

    @classmethod
    def from_env(cls) -> "ReviewSamplingPolicy":
        import os

        raw = os.environ.get("EVAL_REVIEW_SAMPLING", "")
        if not raw:
            return cls()
        try:
            return cls.model_validate(json.loads(raw))
        except ValueError:
            return cls()

    def version(self) -> str:
        return f"sampling-v{self.schemaVersion}:" + hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:16]


def _deterministic_pick(seed: int, key: str) -> float:
    digest = hashlib.sha256(f"{seed}:{key}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def plan_review_tasks(state: RunState, policy: ReviewSamplingPolicy, cases: list[CaseVersion] | None = None) -> list[dict]:
    """效果题才默认送审；规则 FAIL 不等同主观分不可靠。每条 trial 至多一个任务。"""
    if not policy.enabled:
        return []
    plan: list[dict] = []
    by_case = {case.caseId: case for case in cases or []}
    version = policy.version()
    for trial in state.trials:
        case = by_case.get(trial.caseId)
        flow = (case is not None and case.taskType == "agent_flow") or bool(trial.flowLayer)
        grades = [grade for grade in state.grades if grade.trialId == trial.trialId]
        verdict, score = trial.qualityVerdict, trial.weightedScore
        def append(priority, rule):
            plan.append({"trial": trial, "priority": priority, "rule": rule, "basis": version})
        if flow:
            if case and case.needsHumanReview and policy.required != "none":
                append("required", "flagged_case")
            elif verdict == "fail" and (policy.reviewAgentFlowFails or policy.agentFailures) and _deterministic_pick(policy.seed, f"{trial.trialId}:risk") < policy.riskRate:
                append("risk", "agent_flow_failure")
            continue
        # 纯程序规则证据无需走质量分灰区或随机抽检；显式人工标记仍保留。
        if grades and all(grade.source == "rules" and grade.status in {"pass", "fail", "dependency_failed"} for grade in grades) and not (case and case.needsHumanReview):
            continue
        if not policy.includeSynthetic and (state.manifest.mode == "mock" or state.manifest.provider == "mock" or state.manifest.grading.get("judge", "stub") == "stub"):
            continue
        if trial.status not in {"completed", "failed", "timed_out"}:
            continue
        if policy.required != "none":
            if case and case.needsHumanReview:
                append("required", "flagged_case")
                continue
            if any(grade.status == "error" for grade in grades) or any(attempt.trialId == trial.trialId and attempt.callSite == "judge" and attempt.status == "failed" for attempt in state.attempts):
                append("required", "judge_error")
                continue
            if trial.status == "completed" and verdict in {None, "undetermined"}:
                append("required", "judge_unreliable")
                continue
            if score is not None:
                if policy.gateScoreThreshold is not None and verdict in {"pass", "fail"} and ((score >= policy.gateScoreThreshold) != (verdict == "pass")):
                    append("required", "score_gate_conflict")
                    continue
                if abs(score - policy.riskThreshold) <= policy.riskMargin + 1e-12:
                    append("required", "score_ambiguous")
                    continue
            if policy.required == "all":
                append("required", "required_all")
                continue
        conflict = any(a.checkId == b.checkId and a.evaluator != b.evaluator and a.status in {"pass", "fail"} and b.status in {"pass", "fail"} and (a.status != b.status or a.observed != b.observed) for a in grades for b in grades)
        if _deterministic_pick(policy.seed, f"{trial.trialId}:risk") < policy.riskRate:
            if conflict:
                append("risk", "judge_disagreement")
                continue
            if score is not None and policy.grayMin <= score <= policy.grayMax:
                append("risk", "score_gray")
                continue
        if verdict == "pass" and trial.status == "completed" and _deterministic_pick(policy.seed, f"{trial.trialId}:random") < policy.randomRate:
            append("random", "random")
    return plan


def create_scheduled_reviews(
    review_store: "ReviewStore", state: RunState, policy: ReviewSamplingPolicy | None = None
) -> list:
    policy = policy or ReviewSamplingPolicy()
    grades_by_trial: dict[str, list[dict]] = {}
    for grade in state.grades:
        grades_by_trial.setdefault(grade.trialId, []).append(grade.model_dump())
    from .datasets import DatasetStore
    cases = DatasetStore.load_run_snapshot(review_store.root.parent / "runs" / state.manifest.runId)
    created = []
    for item in plan_review_tasks(state, policy, cases):
        trial = item["trial"]
        task = review_store.create(
            state.manifest.runId,
            trial.trialId,
            trial.caseId,
            artifact_ids=list(trial.artifactIds),
            grade_refs=[
                {"gradeId": grade["gradeId"], "evaluator": grade.get("evaluator")}
                for grade in grades_by_trial.get(trial.trialId, [])
            ],
            rubric_version=str(state.manifest.grading.get("rubricVersion", "1")),
            priority=item["priority"],
            sampling={"rule": item["rule"], "policyVersion": item["basis"]},
        )
        created.append(task)
    return created
