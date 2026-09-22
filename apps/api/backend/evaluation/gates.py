"""发布门禁与版本比较(技术方案 §18/§12.3,验收 GATE-01/METRIC-02)。

门禁纪律:
- pass/block/inconclusive 三态;inconclusive 同样不允许自动发布,但区别展示。
- mock/stub 证据不能支撑质量结论(§1.3):allowMockQualityEvidence=false 时
  mock run 的质量门禁只能是 inconclusive。
- 必审未完成、存在争议、人工最终裁决失败或未定,都不允许通过(§11):
  门禁读取审核任务与追加式裁决,失败裁决 → block;未定/未完成 → inconclusive。
- 裁判未经独立人工校准(或校准未达标)→ 质量门禁 inconclusive,不默认通过(§11.3)。
- 回放完整性失败(replay.verification=failed)→ block,差异可追溯到 state 证据。
- 判定引用固定 policy 版本与报告/审核水位;新审核产生新判定,不改写旧判定;
  例外决定追加记录,不改写原判定。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..common import atomic_write_text, now
from .models import RunState, canonical_hash
from .reports import build_report

DEFAULT_POLICY: dict[str, Any] = {
    "schemaVersion": 2,
    "policyId": "release-core",
    "version": 2,
    "integrity": {
        "requireFrozenDataset": True,
        "requireCompleteCoreEvidence": True,
        "allowMockQualityEvidence": False,
    },
    "execution": {"maxCriticalFlowFailures": 0},
    "human": {
        "requireNoUnresolvedDisputes": True,
        "requireRequiredReviewsAdjudicated": True,
        "failOnHumanFailVerdict": True,
    },
    "quality": {
        # 裁判校准要求:无校准记录或缺陷召回低于阈值 → inconclusive(§11.3)
        "requireJudgeCalibration": True,
        "minDefectRecall": 0.8,
    },
    "replay": {"blockOnIntegrityFailure": True},
    "performance": {"maxLatencyP95Ms": None},  # 完成 trial 的 p95 延迟上限;None 表示不设阈值
    "cost": {"requireKnownOrBoundedCost": True},
}

CRITICAL_FLOW_CATEGORIES = {"server_error", "auth", "invalid_request", "unknown"}


class GateDecision(BaseModel):
    schemaVersion: int = 2
    gateId: str
    runId: str
    policyId: str
    policyVersion: int
    verdict: str  # pass / block / inconclusive
    reasons: list[str] = Field(default_factory=list)
    inputWatermark: dict = Field(default_factory=dict)  # planHash/报告版本/审核水位/判定来源
    decidedAt: str = Field(default_factory=now)


def _review_findings(store: Path, run_id: str, human_policy: dict) -> tuple[list[str], list[str], list[str]]:
    """审核侧发现:block 理由、inconclusive 理由与参与水位 hash 的快照。"""
    from .reviews import ReviewStore

    reasons_block: list[str] = []
    reasons_inconclusive: list[str] = []
    tasks = [task for task in ReviewStore(store).list() if task.runId == run_id]
    review_snapshot = []
    store_api = ReviewStore(store)
    for task in tasks:
        adjudication = store_api.latest_adjudication(task.reviewTaskId)
        opinions = store_api._opinions(task.reviewTaskId)
        review_snapshot.append(
            {
                "reviewTaskId": task.reviewTaskId,
                "status": task.status,
                "priority": task.priority,
                "adjudication": adjudication.model_dump() if adjudication else None,
                "opinionCount": len(opinions),
            }
        )
        if human_policy.get("requireNoUnresolvedDisputes", True) and task.status == "disputed":
            reasons_inconclusive.append(f"存在未解决的争议审核:{task.reviewTaskId}")
        if human_policy.get("requireRequiredReviewsAdjudicated", True) and task.priority == "required":
            if task.status != "adjudicated" or adjudication is None:
                reasons_inconclusive.append(
                    f"必审任务未完成裁决:{task.reviewTaskId}(状态 {task.status}),不得通过"
                )
                continue
        if human_policy.get("failOnHumanFailVerdict", True) and adjudication is not None:
            if adjudication.finalVerdict == "fail":
                reasons_block.append(
                    f"人工最终裁决失败:{task.reviewTaskId} — {adjudication.reason[:120]}"
                )
            elif adjudication.finalVerdict == "undetermined":
                reasons_inconclusive.append(
                    f"人工裁决未定:{task.reviewTaskId} — {adjudication.reason[:120]}"
                )
    watermark = canonical_hash(review_snapshot)
    return reasons_block, reasons_inconclusive, watermark


def _calibration_findings(store: Path, state: RunState, quality_policy: dict) -> list[str]:
    """vlm 裁判的校准要求:无通过记录的校准 → 质量结论 inconclusive(§11.3)。"""
    if state.manifest.grading.get("judge", "stub") != "vlm":
        return []
    if not quality_policy.get("requireJudgeCalibration", True):
        return []
    from .calibration import CalibrationStore

    records = CalibrationStore(store).list_for_judge(
        state.manifest.grading.get("model", ""),
        rubric_version=str(state.manifest.grading.get("rubricVersion", "1")),
    )
    if not records:
        return ["vlm 裁判缺少与模型/规则版本匹配的人工校准记录,质量结论按 inconclusive 对待"]
    min_recall = float(quality_policy.get("minDefectRecall", 0.8))
    passing = [r for r in records if (r.get("report") or {}).get("defectRecall") is not None and r["report"]["defectRecall"] >= min_recall]
    if not passing:
        return [
            f"vlm 裁判校准未达标(缺陷召回需 ≥ {min_recall}),质量结论按 inconclusive 对待(§11.3)"
        ]
    return []


def evaluate_gate(
    state: RunState, cases, store: Path | None = None, policy: dict | None = None,
    state_extra: dict | None = None,
) -> GateDecision:
    policy = policy or DEFAULT_POLICY
    reasons_block: list[str] = []
    reasons_inconclusive: list[str] = []
    report = build_report(state, cases)
    integrity = policy.get("integrity", {})

    if integrity.get("requireFrozenDataset") and state.manifest.dataset.get("contentHash") in (
        None,
        "",
        "unavailable",
    ):
        reasons_inconclusive.append("数据集快照缺失,证据链不完整")
    if integrity.get("allowMockQualityEvidence") is False:
        if state.manifest.provider == "mock":
            reasons_inconclusive.append("mock 生成证据不能支撑质量结论(§1.3)")
    if integrity.get("allowStubJudgeEvidence", False) is False:
        if state.manifest.grading.get("judge", "stub") == "stub":
            reasons_inconclusive.append("stub 判分证据不能支撑质量结论(§1.3)")
    if integrity.get("requireCompleteCoreEvidence") and report["coverageGaps"]:
        reasons_inconclusive.append(
            f"存在 {len(report['coverageGaps'])} 条覆盖缺口(未执行/未判定),判定覆盖率不足"
        )

    execution = policy.get("execution", {})
    critical_failures = sum(
        1
        for trial in state.trials
        if trial.status == "failed" and trial.errorCategory in CRITICAL_FLOW_CATEGORIES
    )
    max_critical = execution.get("maxCriticalFlowFailures")
    if max_critical is not None and critical_failures > max_critical:
        reasons_block.append(f"流程级失败 {critical_failures} 次超过阈值 {max_critical}")

    # 取消/预算中止的 run 不给质量通过
    if state.status in {"cancelled", "stopping", "budget_exhausted"}:
        reasons_block.append(f"run 终态为 {state.status},计划未完整执行")

    replay_policy = policy.get("replay", {})
    extra = state_extra if state_extra is not None else {}
    replay = extra.get("replay") or {}
    if replay.get("verification") == "failed":
        if replay_policy.get("blockOnIntegrityFailure", True):
            reasons_block.append(
                f"回放完整性失败:未消费 {len(replay.get('unconsumedInPlan', []))} 条,"
                f"新增出口 {replay.get('newExternalCalls', 0)} 个(证据见 run state)"
            )
    if replay_policy.get("requireVerification") and state.manifest.mode == "replay" and not replay:
        reasons_inconclusive.append("回放缺少完整性校验结果,证据不完整")

    human_policy = policy.get("human", {}) or {}
    review_watermark = None
    if store is not None:
        # 审核门禁默认开启(fail-closed):policy.human 缺省时按默认规则执行,
        # 显式传入 False 才关闭单项(§11:必审未完成/争议/人工失败都不得默认通过)。
        block, inconclusive, review_watermark = _review_findings(
            store, state.manifest.runId, human_policy
        )
        reasons_block.extend(block)
        reasons_inconclusive.extend(inconclusive)

    reasons_inconclusive.extend(_calibration_findings(store, state, policy.get("quality", {})) if store else [])

    performance = policy.get("performance", {}) or {}
    max_latency = performance.get("maxLatencyP95Ms")
    p95 = report["latencyMs"].get("p95")
    if max_latency is not None and p95 is not None and p95 > max_latency:
        reasons_block.append(f"p95 延迟 {p95}ms 超过阈值 {max_latency}ms")

    if policy.get("cost", {}).get("requireKnownOrBoundedCost"):
        usage = report["usage"]
        if usage.get("unknownCalls"):
            reasons_inconclusive.append(f"{usage['unknownCalls']} 笔调用费用未知,成本不可判定")

    if reasons_block:
        verdict = "block"
    elif reasons_inconclusive:
        verdict = "inconclusive"
    else:
        # 无阻断且无缺口:还需计划内全部通过才允许 pass;未全过即 block。
        if report["denominators"]["pass"] == report["denominators"]["plan"]:
            verdict = "pass"
        else:
            reasons_block.append("计划内存在未通过项")
            verdict = "block"
    return GateDecision(
        gateId=f"gate-{uuid.uuid4().hex[:12]}",
        runId=state.manifest.runId,
        policyId=policy.get("policyId", "release-core"),
        policyVersion=int(policy.get("version", 2)),
        verdict=verdict,
        reasons=reasons_block + reasons_inconclusive,
        inputWatermark={
            "planHash": state.manifest.planHash,
            "reportHash": canonical_hash({k: v for k, v in report.items() if k != "reportVersion"}),
            "reviewSnapshotHash": review_watermark,
            "mode": state.manifest.mode,
            "provider": state.manifest.provider,
        },
    )


class GateStore:
    """gates/<gateId>.json 追加保存;已发布判定不被改写(§11.4)。"""

    def __init__(self, store: Path):
        self.root = store / "gates"

    def save(self, decision: GateDecision) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{decision.gateId}.json"
        atomic_write_text(
            path, json.dumps(decision.model_dump(), ensure_ascii=False, indent=2)
        )
        return path

    def list_for_run(self, run_id: str) -> list[GateDecision]:
        if not self.root.is_dir():
            return []
        result = []
        for path in sorted(self.root.glob("*.json")):
            decision = GateDecision.model_validate(json.loads(path.read_text(encoding="utf-8")))
            if decision.runId == run_id:
                result.append(decision)
        return result

    def add_exception(
        self, gate_id: str, decided_by: str, reason: str, valid_until: str, watch_plan: str
    ) -> dict:
        """发布例外:写明证据、原因、范围、有效期与观察计划;不改写原判定(§18.2)。"""
        path = self.root / f"{gate_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"门禁判定不存在:{gate_id}")
        exception = {
            "exceptionId": f"exc-{uuid.uuid4().hex[:8]}",
            "decidedBy": decided_by,
            "reason": reason,
            "validUntil": valid_until,
            "watchPlan": watch_plan,
            "at": now(),
        }
        decision = json.loads(path.read_text(encoding="utf-8"))
        decision.setdefault("exceptions", []).append(exception)
        atomic_write_text(path, json.dumps(decision, ensure_ascii=False, indent=2))
        return exception
