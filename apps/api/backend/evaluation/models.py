"""评测领域实体与状态轴(技术方案-完整评测体系 v1.0 §5)。

不可变约定(§6):run/数据集 manifest 冻结后不再修改;重跑、重新评分、审核修订
都创建新记录;事件只追加。字段命名与技术方案示例保持一致(camelCase),
便于后续 /api/evals/v1 与前端直接消费。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 2
EVENT_SCHEMA_VERSION = 1

# 状态轴(§5.4):执行、质量、审核、门禁、记录完整性分开建模,不互相覆盖。
RunStatus = Literal[
    "draft",
    "validating",
    "queued",
    "running",
    "stopping",
    "completed",
    "failed",
    "cancelled",
    "budget_exhausted",
    "interrupted",
]
TrialStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "blocked",
    "unknown_external",
]
GradeStatus = Literal[
    "pass", "fail", "inconclusive", "error", "dependency_failed", "not_applicable"
]
ExecutionMode = Literal["mock", "replay", "live"]
TaskType = Literal[
    "text_to_image", "image_to_image", "image_edit", "text_to_video", "image_to_video"
]
CheckKind = Literal["boolean", "integer", "number", "text"]
DatasetSplit = Literal[
    "dev", "holdout", "regression", "calibration_train", "calibration_dev", "calibration_test"
]
SuiteType = Literal[
    "smoke", "regression", "capability", "judge_calibration", "reliability", "acceptance"
]
FixtureSource = Literal["internal", "synthetic", "recorded", "real", "imported"]
GradeSource = Literal["rules", "stub", "vlm", "human", "legacy"]
TrialQualityVerdict = Literal["pass", "fail", "undetermined"]

CASE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{2,63}")
CHECK_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")

# 评分器注册(§10.6)的最小登记表:更换规则或模型必须更换版本。
EVALUATOR_TYPED_RULES = {"id": "typed-rules", "version": "1", "rubricVersion": "1"}
EVALUATOR_STUB = {"id": "stub-judge", "version": "1", "rubricVersion": "1"}
EVALUATOR_LEGACY = {"id": "legacy-expect-substring", "version": "unknown", "rubricVersion": "unknown"}


def canonical_hash(data: Any) -> str:
    """排序键的规范化 JSON 哈希;数组顺序保留(§8.3:数组/消息顺序不可重排)。"""
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def trial_identity(run_id: str, case_id: str, case_version: int, repetition: int) -> str:
    """§6.3 唯一约束 runId+caseVersion+repetitionIndex 的确定性 trialId。"""
    key = f"{run_id}|{case_id}|{case_version}|{repetition}"
    return f"trial-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


class Check(BaseModel):
    """类型化检查项(§5.2):判定由结构化 observed 与 expected 的类型化比较完成,
    禁止子串包含判分(§10.4)。"""

    # 默认值也参与验证,保证 JSON 往返前后 model_dump 规范一致(contentHash 稳定的前提)。
    model_config = ConfigDict(validate_default=True)

    id: str
    kind: CheckKind
    question: str
    expected: bool | int | float | str
    required: bool = True
    weight: float = Field(default=1.0, gt=0)
    dependsOn: list[str] = Field(default_factory=list)
    tolerance: float | None = None  # 仅 number:|observed-expected| <= tolerance 视为通过

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not CHECK_ID_PATTERN.fullmatch(value):
            raise ValueError(f"检查项 ID 不合法:{value}")
        return value

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("检查项问题不能为空")
        return value.strip()

    @model_validator(mode="after")
    def _typed_expected(self) -> "Check":
        if self.kind == "boolean" and not isinstance(self.expected, bool):
            raise ValueError(f"检查项 {self.id}:boolean 期望值必须是 true/false")
        if self.kind == "integer" and (isinstance(self.expected, bool) or not isinstance(self.expected, int)):
            raise ValueError(f"检查项 {self.id}:integer 期望值必须是整数")
        if self.kind == "number" and (
            isinstance(self.expected, bool) or not isinstance(self.expected, (int, float))
        ):
            raise ValueError(f"检查项 {self.id}:number 期望值必须是数值")
        if self.kind == "text" and not isinstance(self.expected, str):
            raise ValueError(f"检查项 {self.id}:text 期望值必须是字符串")
        if self.kind != "number" and self.tolerance is not None:
            raise ValueError(f"检查项 {self.id}:仅 number 检查允许 tolerance")
        return self


class CaseInput(BaseModel):
    prompt: str
    referenceArtifactIds: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class ExpectedOutcome(BaseModel):
    execution: Literal["completed"] = "completed"
    quality: Literal["all_required_pass"] = "all_required_pass"


class Provenance(BaseModel):
    """夹具与用例来源必须标明 synthetic/recorded/real(P4 前提,§1.3)。"""

    source: FixtureSource = "internal"
    sourceTraceId: str | None = None
    note: str | None = None


class CaseVersion(BaseModel):
    schemaVersion: Literal[2] = 2
    caseId: str
    version: int = Field(default=1, ge=1)
    language: str = "zh-CN"
    taskType: TaskType = "text_to_image"
    tags: list[str] = Field(default_factory=list)
    suites: list[SuiteType] = Field(default_factory=lambda: ["smoke"])
    title: str = ""
    input: CaseInput
    checks: list[Check] = Field(min_length=1)
    expectedOutcome: ExpectedOutcome = ExpectedOutcome()
    provenance: Provenance = Provenance()
    contentHash: str = ""

    @field_validator("caseId")
    @classmethod
    def _case_id(cls, value: str) -> str:
        if not CASE_ID_PATTERN.fullmatch(value):
            raise ValueError(f"caseId 不合法:{value}(小写字母/数字/连字符,3-64 位)")
        return value

    @field_validator("input")
    @classmethod
    def _prompt(cls, value: CaseInput) -> CaseInput:
        if not value.prompt.strip():
            raise ValueError("用例提示词不能为空")
        return value

    @model_validator(mode="after")
    def _graph(self) -> "CaseVersion":
        ids = [check.id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError(f"用例 {self.caseId}:检查项 ID 重复")
        known = set(ids)
        for check in self.checks:
            for dep in check.dependsOn:
                if dep not in known:
                    raise ValueError(f"用例 {self.caseId}:检查项 {check.id} 依赖不存在的 {dep}")
            if check.id in check.dependsOn:
                raise ValueError(f"用例 {self.caseId}:检查项 {check.id} 依赖自身")
        if _find_cycle({check.id: check.dependsOn for check in self.checks}):
            raise ValueError(f"用例 {self.caseId}:检查项依赖成环")
        self.contentHash = canonical_hash(self.model_dump(exclude={"contentHash"}))
        return self


def _find_cycle(graph: dict[str, list[str]]) -> bool:
    seen: dict[str, int] = {}

    def visit(node: str) -> bool:
        if seen.get(node) == 1:
            return True
        if seen.get(node) == 2:
            return False
        seen[node] = 1
        if any(visit(dep) for dep in graph.get(node, [])):
            return True
        seen[node] = 2
        return False

    return any(visit(node) for node in graph if seen.get(node) is None)


class DatasetManifest(BaseModel):
    """冻结的数据集版本(§4.2):不可变,重复冻结同内容幂等,同版本不同内容报错。"""

    schemaVersion: Literal[2] = 2
    datasetId: str
    version: int = Field(ge=1)
    split: DatasetSplit = "dev"
    language: str = "zh-CN"
    frozenAt: str
    caseCount: int
    contentHash: str
    caseHashes: list[str]
    sourcePath: str | None = None
    sourceContentHash: str | None = None


class TrialPlanLine(BaseModel):
    caseId: str
    caseVersion: int
    caseContentHash: str
    repetitionIndex: int = Field(ge=0)
    requestedSeed: int


class RunManifest(BaseModel):
    """Run 创建即冻结的不可变实验配置(§5.3)。"""

    schemaVersion: Literal[2] = 2
    runId: str
    createdAt: str
    purpose: str = ""
    origin: Literal["evaluation"] = "evaluation"
    mode: ExecutionMode
    provider: str
    model: str | None = None
    dataset: dict[str, Any]  # datasetId/version/contentHash/frozenAt
    plan: list[TrialPlanLine]
    planHash: str = ""
    grading: dict[str, Any] = Field(
        default_factory=lambda: {"judge": "stub", "rubricVersion": "1"}
    )
    budget: dict[str, Any] = Field(
        default_factory=lambda: {"currency": "CNY", "maxCost": "0.00", "maxExternalCalls": 0}
    )
    legacy: bool = False
    parentRunId: str | None = None
    code: dict[str, Any] = Field(default_factory=dict)
    # 非密钥沙盒配置快照(provider/cloudVendor/cloudBaseUrl/cloudModel 等);密钥永不入 manifest(§16)。
    sandboxConfig: dict[str, Any] = Field(default_factory=dict)
    recordingId: str | None = None
    seedPolicy: str = "deterministic"
    # 压缩实验配置(§13.5):默认关闭;启用时冻结压缩器版本。
    compression: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})

    @model_validator(mode="after")
    def _hash(self) -> "RunManifest":
        if not self.planHash:
            self.planHash = canonical_hash([line.model_dump() for line in self.plan])
        return self


class ArtifactRecord(BaseModel):
    """内容寻址素材(§5.1 Artifact):哈希即身份,位置可由 project+sha256 推导。"""

    schemaVersion: Literal[2] = 2
    artifactId: str
    sha256: str
    mime: str
    size: int
    kind: Literal["image", "video", "mask", "reference", "other"] = "image"
    createdAt: str
    originRunId: str | None = None


class UsageRecord(BaseModel):
    """§13.1:未知保持 None,绝不冒充 0。"""

    source: Literal["provider_reported", "locally_estimated", "unknown"] = "unknown"
    textTokens: int | None = None
    images: int | None = None
    videoSeconds: float | None = None
    raw: dict[str, Any] | None = None


class CostRecord(BaseModel):
    currency: str = "CNY"
    amount: float | None = None
    basis: Literal["billed", "estimated", "unknown"] = "unknown"


class Attempt(BaseModel):
    """trial 内一次外部请求尝试(§5.1 Attempt);重试计费与恢复在 P1 扩展。"""

    schemaVersion: Literal[2] = 2
    attemptId: str
    trialId: str
    callSite: str = "generation"
    attemptIndex: int = Field(default=0, ge=0)
    status: Literal["succeeded", "failed", "cancelled", "unknown_external", "not_started"]
    usage: UsageRecord | None = None
    cost: CostRecord | None = None
    externalTaskId: str | None = None
    error: str | None = None
    startedAt: str | None = None
    finishedAt: str | None = None


class GradeEvidence(BaseModel):
    artifactId: str | None = None
    frameTimeMs: int | None = None
    region: str | None = None
    note: str | None = None


class Grade(BaseModel):
    """单检查项判定(§5.5):observed 为裁判结构化原始回答,status 为依赖规则
    计算后的有效判定,两者分离以满足 GRADE-02(保留矛盾证据,不覆盖原始回答)。"""

    schemaVersion: Literal[2] = 2
    gradeId: str
    trialId: str
    checkId: str
    evaluator: dict[str, str]
    status: GradeStatus
    observed: bool | int | float | str | None = None
    expected: bool | int | float | str | None = None
    score: float | None = None
    evidence: list[GradeEvidence] = Field(default_factory=list)
    source: GradeSource
    sourceGradeId: str | None = None
    cacheHit: bool = False


class EvalEvent(BaseModel):
    """持久化事件(§14.3):eventId 去重,sequence 排序,只追加。"""

    schemaVersion: Literal[1] = 1
    eventId: str
    sequence: int
    type: str
    runId: str
    trialId: str | None = None
    traceId: str | None = None
    stepId: str | None = None
    timestamp: str
    payload: dict[str, Any] = Field(default_factory=dict)


class Trial(BaseModel):
    schemaVersion: Literal[2] = 2
    trialId: str
    runId: str
    caseId: str
    caseVersion: int
    repetitionIndex: int = Field(ge=0)
    status: TrialStatus = "pending"
    requestedSeed: int
    resolvedSeed: int | None = None
    jobId: str | None = None
    externalTaskId: str | None = None
    artifactIds: list[str] = Field(default_factory=list)
    # 视频任务的抽帧证据(可复现,版本化);空列表=未抽帧或环境不可得(事件里说明原因)。
    frameArtifactIds: list[str] = Field(default_factory=list)
    attemptIds: list[str] = Field(default_factory=list)
    qualityVerdict: TrialQualityVerdict | None = None
    weightedScore: float | None = None
    error: str | None = None
    errorCategory: str | None = None
    queueMs: int | None = None
    latencyMs: int | None = None
    startedAt: str | None = None
    finishedAt: str | None = None


class RunState(BaseModel):
    """run 的可重建状态投影(§6.2:投影从事件/记录重建,不是第二事实源)。"""

    manifest: RunManifest
    trials: list[Trial]
    grades: list[Grade] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    status: RunStatus = "queued"
