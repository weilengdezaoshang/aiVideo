"""提示词压缩配对实验(技术方案 §13.5)。

- 配对设计:同一数据集在"原始 prompt"与"压缩 prompt"下各执行一个 run,
  用例/seed/模型/裁判配置冻结一致,差异只来自压缩(可归因)。
- 计量诚实性:token 以服务商 usage 为准;未知显示未知,字符数不冒充 token 数(§13.1)。
- 质量变化与费用变化同时报告;质量下降超过阈值的配置禁止推广。
- 默认关闭:压缩可能改变语义,必须显式启用并冻结压缩器版本(§13.5)。
"""

from __future__ import annotations

from .models import RunState
from .reports import build_report


def _plan_completion(state: RunState):
    """计划完成率(N_pass/N_plan);复用 reports 分母口径,分母 0 → N/A。"""
    report = build_report(state, [])
    return report["metrics"]["planCompletionRate"]


def compression_summary(baseline: RunState, compressed: RunState) -> dict:
    """对比原始 run 与压缩 run:成本、质量与覆盖,全部以证据为准。

    - 质量以条件质量通过率(确定分母)比较;任一侧无确定分母 → N/A。
    - 费用以预算账本摘要(外部注入)比较;mock/无账本 → 费用 N/A,不显示 0 冒充。
    - token 只统计 provider_reported 的 usage;未知保持 unknown,不用字符数估算。
    """
    if baseline.manifest.runId == compressed.manifest.runId:
        raise ValueError("压缩对比需要两个不同的 run")

    def verdict_counts(state: RunState) -> dict:
        counts = {"pass": 0, "fail": 0, "undetermined": 0}
        for trial in state.trials:
            if trial.qualityVerdict in counts:
                counts[trial.qualityVerdict] += 1
        return counts

    base_counts = verdict_counts(baseline)
    comp_counts = verdict_counts(compressed)

    def pass_rate(counts: dict) -> float | None:
        determined = counts["pass"] + counts["fail"]
        return round(counts["pass"] / determined, 4) if determined else None

    base_rate = pass_rate(base_counts)
    comp_rate = pass_rate(comp_counts)

    def token_usage(state: RunState) -> dict:
        known = 0
        unknown = 0
        for attempt in state.attempts:
            if attempt.usage is None:
                continue
            if attempt.usage.source == "provider_reported" and attempt.usage.textTokens is not None:
                known += attempt.usage.textTokens
            else:
                unknown += 1
        return {"providerReportedTokens": known or None, "unknownUsageCalls": unknown or None}

    base_tokens = token_usage(baseline)
    comp_tokens = token_usage(compressed)

    def call_stats(state: RunState) -> dict:
        from collections import Counter

        sites = Counter(attempt.callSite for attempt in state.attempts)
        cache_hits = sum(
            1 for grade in state.grades if grade.cacheHit
        )
        return {"attempts": sum(sites.values()), "byCallSite": dict(sites), "cacheHits": cache_hits}

    quality_delta = (
        round(comp_rate - base_rate, 4) if base_rate is not None and comp_rate is not None else None
    )
    compressor = compressed.manifest.compression or {}
    return {
        "schemaVersion": 1,
        "baselineRunId": baseline.manifest.runId,
        "compressedRunId": compressed.manifest.runId,
        "compressorVersion": compressor.get("compressorVersion"),
        "compressionEnabled": bool(compressor.get("enabled")),
        "quality": {
            "baselinePassRate": base_rate,
            "compressedPassRate": comp_rate,
            "delta": quality_delta,
            "baselineCounts": base_counts,
            "compressedCounts": comp_counts,
            "note": "通过率只计确定判定(pass+fail);未定/未评保留在覆盖缺口(§12.1)",
        },
        "tokens": {
            "baseline": base_tokens,
            "compressed": comp_tokens,
            "delta": (
                comp_tokens["providerReportedTokens"] - base_tokens["providerReportedTokens"]
                if base_tokens["providerReportedTokens"] is not None
                and comp_tokens["providerReportedTokens"] is not None
                else None
            ),
            "note": "token 仅统计服务商 reported usage;未知保持未知,不用字符数折算(§13.1)",
        },
        "calls": {"baseline": call_stats(baseline), "compressed": call_stats(compressed)},
        "coverage": {
            "baselinePlanCompletion": _plan_completion(baseline),
            "compressedPlanCompletion": _plan_completion(compressed),
            "note": "计划完成率(N_pass/N_plan);未执行/未评保留在覆盖缺口(§12.1)",
        },
        "cost": {
            "baselineSettled": None,
            "compressedSettled": None,
            "delta": None,
            "note": "费用由预算账本摘要注入;缺失时为 N/A,不显示 0(§13.1)",
        },
        "promote": None,
        "promoteRule": "质量下降超过阈值(默认 0.02)禁止推广;费用与 token 证据一并提供",
    }


def apply_cost(summary: dict, baseline_budget: dict | None, compressed_budget: dict | None) -> dict:
    """把两个 run 的账本摘要并入压缩报告;未知费用保持 None。"""
    baseline_settled = (baseline_budget or {}).get("settled")
    compressed_settled = (compressed_budget or {}).get("settled")
    delta = (
        round(compressed_settled - baseline_settled, 6)
        if isinstance(baseline_settled, (int, float)) and isinstance(compressed_settled, (int, float))
        else None
    )
    summary["cost"] = {
        "baselineSettled": baseline_settled,
        "compressedSettled": compressed_settled,
        "delta": delta,
        "note": "已结算口径(估算),预留/未知另见各 run 预算端点(§13.1)",
    }
    return summary


def apply_promotion_gate(summary: dict, max_quality_drop: float = 0.02) -> dict:
    """质量下降超阈值的压缩配置禁止推广;证据不足同样不得推广。"""
    delta = (summary.get("quality") or {}).get("delta")
    if delta is None:
        summary["promote"] = False
        summary["promoteReason"] = "质量变化无法确定(确定分母不足),不得推广"
        return summary
    if delta < -max_quality_drop:
        summary["promote"] = False
        summary["promoteReason"] = f"质量下降 {-delta} 超过阈值 {max_quality_drop},禁止推广"
        return summary
    summary["promote"] = True
    summary["promoteReason"] = "质量变化在允许范围内;推广仍需成本与覆盖证据支持"
    return summary
