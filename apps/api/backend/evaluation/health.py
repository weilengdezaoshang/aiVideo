"""运维健康指标(技术方案 §17.4):聚合只读读模型,不创建定时任务或通知。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ..common import now


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def evaluation_health(store: Path, now_ts: str | None = None) -> dict:
    """§17.4 建议指标的可实现子集:队列/卡住/未知上游/预留未结/裁判错误/待审时长/磁盘。"""
    current = _parse(now_ts or now()) or datetime.now(UTC)
    runs_root = store / "runs"
    queue_depth = 0
    stuck_trials: list[str] = []
    unknown_external = 0
    budget_unsettled = 0
    budget_unknown = 0
    judge_errors = 0
    judged_trials = 0
    oldest_pending_review_hours: float | None = None

    for state_file in runs_root.glob("*/state.json") if runs_root.is_dir() else []:
        run_id = state_file.parent.name
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if state.get("status") in {"running", "queued"}:
            queue_depth += 1
        trials_root = state_file.parent / "trials"
        for trial_file in trials_root.glob("*/trial.json") if trials_root.is_dir() else []:
            trial = json.loads(trial_file.read_text(encoding="utf-8"))
            if trial.get("status") == "running":
                started = _parse(trial.get("startedAt"))
                if started and (current - started).total_seconds() > 3600:
                    stuck_trials.append(f"{run_id}/{trial['trialId']}")
            if trial.get("status") == "unknown_external":
                unknown_external += 1
        grades_root = trials_root
        for grade_file in grades_root.glob("*/grades.json") if grades_root.is_dir() else []:
            grades = json.loads(grade_file.read_text(encoding="utf-8"))
            items = grades.get("grades") or []
            if not items:
                continue
            judged_trials += 1
            if any(grade.get("status") == "error" for grade in items):
                judge_errors += 1
    for budget_file in (store / "budgets").glob("*.jsonl") if (store / "budgets").is_dir() else []:
        for line in budget_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("kind") == "reserve":
                budget_unsettled += 1
            elif entry.get("kind") == "settle_unknown":
                budget_unknown += 1
                budget_unsettled -= 1
    reviews_root = store / "reviews"
    for task_file in reviews_root.glob("review-*/task.json") if reviews_root.is_dir() else []:
        task = json.loads(task_file.read_text(encoding="utf-8"))
        if task.get("status") in {"pending", "claimed", "submitted", "disputed"}:
            created = _parse(task.get("createdAt"))
            hours = (current - created).total_seconds() / 3600 if created else 0
            oldest_pending_review_hours = (
                hours if oldest_pending_review_hours is None else max(oldest_pending_review_hours, hours)
            )
    total_bytes = sum(path.stat().st_size for path in store.rglob("*") if path.is_file())
    return {
        "schemaVersion": 1,
        "generatedAt": now(),
        "activeRuns": queue_depth,
        "stuckTrials": sorted(stuck_trials),
        "unknownExternalTrials": unknown_external,
        "budget": {"unsettledReservations": max(0, budget_unsettled), "unknownOutcomes": budget_unknown},
        "judgeErrorTrials": judge_errors,
        "judgedTrials": judged_trials,
        "oldestPendingReviewHours": round(oldest_pending_review_hours, 2) if oldest_pending_review_hours is not None else None,
        "storeBytes": total_bytes,
        "note": "聚合读模型;告警通道与阈值由部署配置决定,本模块不发送通知(§17.4)",
    }


DEFAULT_ALERT_RULES = {
    "schemaVersion": 1,
    "maxStuckTrials": 0,           # 卡住 trial(>1h running)数量上限
    "maxUnknownExternal": 0,       # 上游未知结果 trial 数量上限
    "maxUnsettledReservations": 0,  # 未结预留数量上限(预算异常)
    "maxOutboxBacklog": 500,       # 观测导出积压上限
    "maxPendingReviewHours": 48,   # 待审最长时间(小时)
    "maxJudgeErrorRate": 0.2,      # 裁判错误率上限
}


def evaluate_alerts(store: Path, rules: dict | None = None) -> dict:
    """可配置告警条件评估:只产出告警清单,不发送任何外部通知(§17.4)。

    rules 为 DEFAULT_ALERT_RULES 的部分覆盖;自动化(发送/删除/付费)默认不启用。
    """
    merged = {**DEFAULT_ALERT_RULES, **(rules or {})}
    snapshot = evaluation_health(store)
    outbox_file = store / "outbox" / "events.jsonl"
    backlog = 0
    if outbox_file.is_file():
        backlog = sum(1 for line in outbox_file.read_text(encoding="utf-8").splitlines() if line.strip())
    alerts = []
    if len(snapshot["stuckTrials"]) > merged["maxStuckTrials"]:
        alerts.append({"code": "STUCK_TRIALS", "message": f"卡住 trial {len(snapshot['stuckTrials'])} 个", "evidence": snapshot["stuckTrials"][:10]})
    if snapshot["unknownExternalTrials"] > merged["maxUnknownExternal"]:
        alerts.append({"code": "UNKNOWN_EXTERNAL", "message": f"上游未知结果 trial {snapshot['unknownExternalTrials']} 个"})
    if snapshot["budget"]["unsettledReservations"] > merged["maxUnsettledReservations"]:
        alerts.append({"code": "BUDGET_UNSETTLED", "message": f"未结预算预留 {snapshot['budget']['unsettledReservations']} 笔", "evidence": snapshot["budget"]})
    if backlog > merged["maxOutboxBacklog"]:
        alerts.append({"code": "OUTBOX_BACKLOG", "message": f"观测导出积压 {backlog} 条", "evidence": {"backlog": backlog}})
    if (
        snapshot["oldestPendingReviewHours"] is not None
        and snapshot["oldestPendingReviewHours"] > merged["maxPendingReviewHours"]
    ):
        alerts.append({"code": "REVIEW_OVERDUE", "message": f"最久待审 {snapshot['oldestPendingReviewHours']}h 超过 {merged['maxPendingReviewHours']}h"})
    if snapshot["judgedTrials"] and snapshot["judgeErrorTrials"] / snapshot["judgedTrials"] > merged["maxJudgeErrorRate"]:
        rate = round(snapshot["judgeErrorTrials"] / snapshot["judgedTrials"], 4)
        alerts.append({"code": "JUDGE_ERROR_RATE", "message": f"裁判错误率 {rate} 超过 {merged['maxJudgeErrorRate']}", "evidence": {"judged": snapshot["judgedTrials"], "errors": snapshot["judgeErrorTrials"]}})
    return {
        "schemaVersion": 1,
        "generatedAt": now(),
        "rules": merged,
        "alerts": alerts,
        "note": "仅评估并列出告警;发送通道与自动处置需显式部署配置,默认不启用(§17.4)",
    }
