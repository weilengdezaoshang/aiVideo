"""漂移抽样与成本效率报表(技术方案 §17.4)。

- 漂移:近 N 个同数据集 completed run 的条件质量通过率滚动对比;相对首个基线
  下降超过阈值即标记漂移,并可为漂移 run 的通过 trial 创建 risk 抽样审核任务
  (抽样依据 rule=drift,可追溯);未定/失败 trial 已由必审策略覆盖,不重复建。
- 成本效率:run 的预算账本已结算金额 ÷ 合格素材数;无账本/零合格素材显示 N/A,
  预留与未知费用单列,不把预留冒充实际费用(§13.1)。
- 两者均为只读报表 + 显式调用;不创建定时任务,不自动发送通知(§17.4)。
"""

from __future__ import annotations

from pathlib import Path

from ..common import now
from .budgets import BudgetLedger
from .datasets import DatasetStore
from .reports import build_report
from .reviews import ReviewStore
from .runner import EvaluationRunner


def cost_efficiency(store: Path, run_id: str, runner: EvaluationRunner | None = None) -> dict:
    """单 run 成本效率:已结算(分口径)与每份合格素材成本;口径缺失显示 N/A。"""
    runner = runner or EvaluationRunner(store.parent.parent, store=store)
    state = runner.load_run(run_id)
    cases = DatasetStore.load_run_snapshot(runner.run_dir(run_id))
    report = build_report(state, cases)
    qualified = report["denominators"]["pass"]
    ledger_file = store / "budgets" / f"{run_id}.jsonl"
    budget = None
    if ledger_file.is_file():
        summary = BudgetLedger.open(store, run_id).summary().model_dump()
        budget = summary
    settled = (budget or {}).get("settled")
    billed = (budget or {}).get("settledBilled")
    estimated = (budget or {}).get("settledEstimated")
    per_qualified = (
        round(settled / qualified, 6)
        if isinstance(settled, (int, float)) and qualified
        else None
    )
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "generatedAt": now(),
        "qualifiedArtifacts": qualified,
        "budget": budget,
        "settled": settled,
        "settledBilled": billed,
        "settledEstimated": estimated,
        "outstandingReserved": (budget or {}).get("outstandingReserved"),
        "unknownCount": (budget or {}).get("unknownCount"),
        "costPerQualifiedArtifact": per_qualified,
        "note": (
            "每份合格素材成本=已结算(估算口径)/N_pass;未结预留与未知费用单列;"
            "无账本或零合格素材显示 N/A,不冒充 0(§13.1)"
        ),
    }


def drift_report(
    store: Path,
    window: int = 5,
    threshold: float = 0.05,
    runner: EvaluationRunner | None = None,
) -> dict:
    """近窗口 completed run 的通过率滚动对比:相对首个基线下降超阈值 → 漂移。

    只比较数据集快照一致的 run(混快照的逐个自成序列,不做跨快照比较,§12.3)。
    """
    if window < 2:
        raise ValueError("窗口至少为 2 个 run")
    runner = runner or EvaluationRunner(store.parent.parent, store=store)
    runs_root = store / "runs"
    if not runs_root.is_dir():
        return {"schemaVersion": 1, "generatedAt": now(), "series": [], "drifted": [], "note": "无运行记录"}
    by_snapshot: dict[str, list[dict]] = {}
    for item in sorted(runner.list_runs(), key=lambda run: run["createdAt"]):
        if item["status"] != "completed" or item["legacy"]:
            continue
        run_id = item["runId"]
        try:
            state = runner.load_run(run_id)
            cases = DatasetStore.load_run_snapshot(runner.run_dir(run_id))
        except (FileNotFoundError, ValueError):
            continue
        report = build_report(state, cases)
        snapshot = state.manifest.dataset.get("contentHash") or ""
        by_snapshot.setdefault(snapshot, []).append(
            {
                "runId": run_id,
                "createdAt": item["createdAt"],
                "passRate": report["metrics"]["conditionalQualityPassRate"],
                "plan": report["denominators"]["plan"],
            }
        )
    series = []
    drifted: list[dict] = []
    for snapshot, entries in sorted(by_snapshot.items()):
        windowed = entries[-window:]
        baseline = next((item["passRate"] for item in windowed if item["passRate"] is not None), None)
        for item in windowed:
            is_drift = (
                baseline is not None
                and item["passRate"] is not None
                and baseline - item["passRate"] > threshold
            )
            row = {**item, "baselinePassRate": baseline, "drift": is_drift}
            series.append(row)
            if is_drift:
                drifted.append(row)
    return {
        "schemaVersion": 1,
        "generatedAt": now(),
        "window": window,
        "threshold": threshold,
        "series": series,
        "drifted": drifted,
        "note": "漂移=相对同快照基线通过率下降超过阈值;混快照序列独立对比(§12.3)",
    }


def create_drift_reviews(
    store: Path, drift: dict, runner: EvaluationRunner | None = None, max_tasks: int = 5
) -> list:
    """为漂移 run 的通过 trial 创建 risk 抽样审核任务(显式调用,不自动定时)。

    失败/未定 trial 已由必审策略覆盖;这里只补抽通过样本,防止漂移被"全通过"掩盖。
    """

    runner = runner or EvaluationRunner(store.parent.parent, store=store)
    reviews = ReviewStore(store)
    created = []
    for row in drift.get("drifted", []):
        run_id = row["runId"]
        try:
            state = runner.load_run(run_id)
        except (FileNotFoundError, ValueError):
            continue
        passing = [t for t in state.trials if t.status == "completed" and t.qualityVerdict == "pass"]
        for trial in passing[:max_tasks]:
            task = reviews.create(
                run_id,
                trial.trialId,
                trial.caseId,
                artifact_ids=list(trial.artifactIds),
                rubric_version=str(state.manifest.grading.get("rubricVersion", "1")),
                priority="risk",
                sampling={"rule": "drift", "policyVersion": f"drift@{drift['generatedAt']}"},
            )
            created.append(task)
    return created

