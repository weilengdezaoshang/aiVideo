"""成本与压缩对比的配对硬校验与推荐(技术方案 §12.3/§13.5)。

纪律:宁可少报一次,也不出误导性的全未知报告。一次合法对比必须同时满足:
1. 同一 datasetId,且用例冻结快照(case set hash)一致;快照不一致禁止总费用对比;
2. 一方 compression=off(基线)、一方 compression=on(对比);
   manifest.compression.role="baseline" 是显式基线标签,等价于基线侧;
3. 两次 run 均成功完成;
4. 两侧均已结算费用(预算账本)且存在服务商上报 token。
缺任一条返回中文 reasons[](snapshot_mismatch / missing_cost / not_compression_pair / ...),
前端据此渲染「无法对比」引导卡,而不是未知数字表。
"""

from __future__ import annotations

from pathlib import Path

from .budgets import BudgetLedger
from .models import RunState

_MISSING_SNAPSHOT = {"", "unavailable"}


def _compression_on(manifest) -> bool:
    return bool((manifest.compression or {}).get("enabled"))


def _snapshot_hash(manifest) -> str:
    return str(manifest.dataset.get("contentHash") or "")


def run_cost_evidence(store: Path, state: RunState) -> dict:
    """run 的费用/token 记账状态:已结算费用来自预算账本,token 只认 provider_reported。"""
    run_id = state.manifest.runId
    ledger_file = store / "budgets" / f"{run_id}.jsonl"
    settled_cost = None
    settled_calls = 0
    if ledger_file.is_file():
        ledger = BudgetLedger.open(store, run_id)
        settled = [item for item in ledger.reservations() if item.status == "settled"]
        if settled:
            settled_cost = ledger.summary().settled
            settled_calls = len(settled)
    tokens = 0
    for attempt in state.attempts:
        usage = attempt.usage
        if (
            usage is not None
            and usage.source == "provider_reported"
            and usage.textTokens is not None
        ):
            tokens += usage.textTokens
    return {
        "settledCost": settled_cost,
        "settledCalls": settled_calls,
        "providerReportedTokens": tokens or None,
        "mode": state.manifest.mode,
    }


def validate_pair(store: Path, baseline: RunState, compressed: RunState) -> list[dict]:
    """压缩对比硬条件;返回中文 reasons[](空列表 = 合法配对)。"""
    reasons: list[dict] = []
    base_m, comp_m = baseline.manifest, compressed.manifest
    if base_m.runId == comp_m.runId:
        reasons.append(
            {"code": "same_run", "message": "基线与对比是同一个运行,请选择两次不同的运行。"}
        )
    base_ds, comp_ds = base_m.dataset.get("datasetId"), comp_m.dataset.get("datasetId")
    if base_ds != comp_ds:
        reasons.append(
            {
                "code": "dataset_mismatch",
                "message": f"两个运行不属于同一数据集({base_ds} 与 {comp_ds}),无法对比。",
            }
        )
    else:
        base_hash, comp_hash = _snapshot_hash(base_m), _snapshot_hash(comp_m)
        if base_hash in _MISSING_SNAPSHOT or comp_hash in _MISSING_SNAPSHOT:
            reasons.append(
                {
                    "code": "snapshot_missing",
                    "message": "至少一侧运行缺少冻结数据集快照,无法保证两侧用例集一致;请用冻结数据集重跑。",
                }
            )
        elif base_hash != comp_hash:
            reasons.append(
                {
                    "code": "snapshot_mismatch",
                    "message": (
                        f"用例快照不一致({base_hash[:12]} 与 {comp_hash[:12]}):"
                        "总费用对比会被数据集差异污染,请用同一冻结版本重跑。"
                    ),
                }
            )
    for label, state in (("基线", baseline), ("压缩", compressed)):
        if state.status != "completed":
            reasons.append(
                {
                    "code": "run_not_success",
                    "message": f"{label}运行「{state.manifest.runId}」状态为 {state.status},未成功完成,不能参与成本对比。",
                }
            )
    base_on, comp_on = _compression_on(base_m), _compression_on(comp_m)
    base_labeled = (base_m.compression or {}).get("role") == "baseline"
    if not (comp_on and (not base_on or base_labeled)):
        reasons.append(
            {
                "code": "not_compression_pair",
                "message": (
                    "压缩对比需要一方关闭压缩(基线)、一方开启压缩(对比);"
                    f"当前基线「{base_m.runId}」压缩{'开' if base_on else '关'}、"
                    f"对比「{comp_m.runId}」压缩{'开' if comp_on else '关'}。"
                ),
            }
        )
    for label, state in (("基线", baseline), ("压缩", compressed)):
        run_id = state.manifest.runId
        evidence = run_cost_evidence(store, state)
        if evidence["settledCost"] is None:
            if state.manifest.mode != "live":
                reasons.append(
                    {
                        "code": "missing_cost",
                        "message": (
                            f"{label}运行「{run_id}」是 {state.manifest.mode} 运行,"
                            "没有真实外部调用与费用记录;成本对比需要带预算记账的 live 运行。"
                        ),
                    }
                )
            else:
                reasons.append(
                    {
                        "code": "missing_cost",
                        "message": f"{label}运行「{run_id}」的预算账本没有已结算费用;请确认创建时配置了预算与单价。",
                    }
                )
        if evidence["providerReportedTokens"] is None:
            reasons.append(
                {
                    "code": "missing_tokens",
                    "message": (
                        f"{label}运行「{run_id}」没有服务商上报的 token"
                        "(未使用真实 VLM 裁判或调用未返回 usage),token 对比无数据。"
                    ),
                }
            )
    return reasons


def pair_coverage(baseline: RunState, compressed: RunState) -> dict:
    """可对齐用例数与两侧计划数;快照一致时差异来自执行失败/取消,不是选题不同。"""
    base_pairs = {
        (t.caseId, t.caseVersion, t.repetitionIndex): t for t in baseline.trials
    }
    comp_pairs = {
        (t.caseId, t.caseVersion, t.repetitionIndex): t for t in compressed.trials
    }
    aligned = 0
    aligned_completed = 0
    for key in set(base_pairs) & set(comp_pairs):
        aligned += 1
        if base_pairs[key].status == "completed" and comp_pairs[key].status == "completed":
            aligned_completed += 1
    return {
        "alignedCases": aligned,
        "alignedCompletedCases": aligned_completed,
        "baselinePlan": len(baseline.manifest.plan),
        "compressedPlan": len(compressed.manifest.plan),
    }


def _counterpart_prefill(current: RunState, compression_enabled: bool) -> dict:
    """重跑引导的预填:同数据集+指定压缩开关;live 运行继承预算与裁判配置。

    配对必须有费用与 token,预填一律 live;mock 源运行的预算/裁判单价由表单默认值补齐。
    """
    manifest = current.manifest
    prefill: dict = {
        "datasetId": manifest.dataset.get("datasetId"),
        "version": manifest.dataset.get("version"),
        "mode": "live",
        "compression": {"enabled": compression_enabled},
        "purpose": "压缩对比基线(未压缩)" if not compression_enabled else "压缩对比运行",
    }
    if manifest.provider:
        prefill["provider"] = manifest.provider
    if manifest.model:
        prefill["model"] = manifest.model
    if manifest.mode == "live":
        prefill["budget"] = manifest.budget
        judge = manifest.grading.get("judge")
        if judge and judge != "stub":
            prefill["judge"] = judge
            prefill["judgeConfig"] = {
                key: value
                for key, value in manifest.grading.items()
                if key not in {"judge", "rubricVersion", "cacheBypass"}
            }
    return prefill


def _self_issues(store: Path, state: RunState) -> list[dict]:
    """当前 run 自身是否具备对比资格(费用/token 已记账)。"""
    issues: list[dict] = []
    run_id = state.manifest.runId
    evidence = run_cost_evidence(store, state)
    if evidence["settledCost"] is None:
        note = (
            f"{run_id} 是 {state.manifest.mode} 运行,无外部费用记录"
            if state.manifest.mode != "live"
            else f"{run_id} 的预算账本没有已结算费用"
        )
        issues.append({"code": "missing_cost", "message": f"本运行缺少已结算费用({note}),不能参与成本对比。"})
    if evidence["providerReportedTokens"] is None:
        issues.append(
            {
                "code": "missing_tokens",
                "message": f"本运行「{run_id}」没有服务商上报的 token,token 对比无数据。",
            }
        )
    return issues


def suggest_pair(runner, store: Path, run_id: str | None = None) -> dict:
    """为指定 run(缺省取全库最新)推荐压缩配对;没有合法对手时返回重跑引导。"""
    completed = sorted(
        (item for item in runner.list_runs() if item.get("status") == "completed"),
        key=lambda item: (item["createdAt"], item["runId"]),
        reverse=True,
    )
    if run_id is None:
        # 默认推荐:最新的自带合法配对的成功运行;都没有则给最新运行的重跑引导。
        for item in completed:
            result = suggest_pair(runner, store, item["runId"])
            if result.get("pairRunId"):
                return result
        if not completed:
            return {
                "currentRunId": None,
                "currentRole": None,
                "pairRunId": None,
                "candidates": [],
                "reasons": [
                    {
                        "code": "no_completed_runs",
                        "message": "暂无已成功的运行;先完成一次评测再进行成本对比。",
                    }
                ],
                "guidance": None,
                "coverage": None,
            }
        return suggest_pair(runner, store, completed[0]["runId"])

    current = runner.load_run(run_id)
    current_on = _compression_on(current.manifest)
    role = "compressed" if current_on else "baseline"
    wanted_on = not current_on
    dataset_id = current.manifest.dataset.get("datasetId")

    candidates = []
    for item in completed:
        if item["runId"] == run_id:
            continue
        state = runner.load_run(item["runId"])
        if state.manifest.dataset.get("datasetId") != dataset_id:
            continue
        if _compression_on(state.manifest) != wanted_on:
            continue
        base, comp = (state, current) if current_on else (current, state)
        reasons = validate_pair(store, base, comp)
        candidates.append(
            {
                "runId": item["runId"],
                "createdAt": item["createdAt"],
                "role": "baseline" if current_on else "compressed",
                "valid": not reasons,
                "reasons": reasons,
            }
        )
    result = {
        "currentRunId": run_id,
        "currentRole": role,
        "pairRunId": None,
        "candidates": candidates,
        "reasons": [],
        "guidance": None,
        "coverage": None,
    }
    valid = [item for item in candidates if item["valid"]]
    if valid:
        best = max(valid, key=lambda item: (item["createdAt"], item["runId"]))
        other = runner.load_run(best["runId"])
        base, comp = (other, current) if current_on else (current, other)
        result["pairRunId"] = best["runId"]
        result["coverage"] = pair_coverage(base, comp)
        return result

    # 没有合法配对:解释原因并给重跑引导。
    self_issues = _self_issues(store, current)
    if self_issues:
        result["reasons"] = self_issues
        result["guidance"] = {
            "action": "rerun_current_live",
            "message": (
                "本运行缺少费用/token 记账(mock 运行无外部调用,或未配置预算与真实裁判),"
                "无法参与成本对比;请先用同一数据集与压缩开关重跑一次带预算的 live 运行。"
            ),
            "prefill": _counterpart_prefill(current, current_on),
        }
        return result
    if candidates:
        # 有反向压缩候选但都不合法:逐条说明差在哪,便于修复(重跑快照/补记账)。
        newest = max(candidates, key=lambda item: (item["createdAt"], item["runId"]))
        result["reasons"] = [
            {
                "code": item["code"],
                "message": f"候选运行「{newest['runId']}」不能配对:{item['message']}",
            }
            for item in newest["reasons"]
        ]
    else:
        result["reasons"] = [
            {
                "code": "no_counterpart",
                "message": (
                    f"没有找到与「{run_id}」同数据集、{'未压缩基线' if current_on else '开启压缩'}的成功运行。"
                ),
            }
        ]
    result["guidance"] = {
        "action": "create_counterpart",
        "message": (
            "本运行已开启压缩,先用同一数据集跑一次关闭压缩的 live 基线,再回来对比。"
            if current_on
            else "本运行未压缩,可作为基线;先跑一次同数据集、开启压缩的 live 运行,再回来对比。"
        ),
        "prefill": _counterpart_prefill(current, wanted_on),
    }
    return result
