"""按运行快照选择上次成功基线，不改变比较算法。"""
from __future__ import annotations


def suggest_baseline(runner, run_id: str | None = None) -> dict:
    runs = sorted((r for r in runner.list_runs() if r["status"] == "completed"),
                  key=lambda r: (r["createdAt"], r["runId"]), reverse=True)
    states = {r["runId"]: runner.load_run(r["runId"]) for r in runs}

    def previous(current):
        state = states[current["runId"]]
        return next((r for r in runs if (r["createdAt"], r["runId"]) < (current["createdAt"], current["runId"])
                     and states[r["runId"]].manifest.dataset.get("datasetId") == state.manifest.dataset.get("datasetId")), None)

    current = next((r for r in runs if r["runId"] == run_id), None) if run_id else next(
        (r for r in runs if previous(r)), runs[0] if runs else None)
    baseline = previous(current) if current else None
    return {"candidateRunId": current["runId"] if current else None,
            "baselineRunId": baseline["runId"] if baseline else None,
            "reason": None if baseline else "暂无更早的同数据集/套件成功运行，请再运行一次或手选基线。",
            "options": [r for r in runs if not current or r["runId"] != current["runId"]]}
