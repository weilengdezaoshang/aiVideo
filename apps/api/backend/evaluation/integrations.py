"""第三方集成适配(技术方案 §3.3):本项目证据库是唯一事实源。

- Promptfoo:Python 只读结果 provider——读取已封存 trial 输出与评分,
  返回素材引用、判分结果与详情链接;不做任何生成或判分(不重复记账)。
- Langfuse:outbox 导出器——把评测事件翻译为 Langfuse trace 摄入格式,
  异步、有界重试;凭据从环境读取,缺失时导出跳过并明确报告(待真实验收)。
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from .outbox import Outbox


def build_promptfoo_tests(store: Path, run_id: str) -> dict:
    """生成 Promptfoo 结果视角:prompts=provider+mode,tests=逐 trial 已有判定。

    只映射已有结论,不调用模型;assertion 的 metric 指向项目内 gradeId 以便回查。
    """
    from .runner import EvaluationRunner

    runner = EvaluationRunner(Path(__file__).resolve().parents[4], store=store)
    state = runner.load_run(run_id)
    grades_by_trial: dict[str, list] = {}
    for grade in state.grades:
        grades_by_trial.setdefault(grade.trialId, []).append(grade)
    results = []
    for trial in state.trials:
        trial_grades = grades_by_trial.get(trial.trialId, [])
        results.append(
            {
                "id": trial.trialId,
                "caseId": trial.caseId,
                "status": trial.status,
                "qualityVerdict": trial.qualityVerdict,
                "weightedScore": trial.weightedScore,
                "artifactIds": trial.artifactIds,
                "checks": [
                    {
                        "checkId": grade.checkId,
                        "status": grade.status,
                        "score": grade.score,
                        "source": grade.source,
                        "gradeRef": grade.gradeId,
                    }
                    for grade in trial_grades
                ],
                "detailUrl": f"/evals?tab=runs&runId={state.manifest.runId}&trialId={trial.trialId}",
            }
        )
    return {
        "provider": "frayune-evidence(只读)",
        "runId": run_id,
        "mode": state.manifest.mode,
        "note": "只读展示项目已有判定;不在 Promptfoo 内重新生成或重新判分(§3.3)",
        "results": results,
    }


def write_promptfoo_config(store: Path, run_id: str, target: Path) -> Path:
    """写一个可直接运行的 Promptfoo 配置:provider 指向本项目的只读结果脚本。"""
    config = {
        "description": f"FRAYUNE 证据只读展示:{run_id}",
        "prompts": [f"file://promptfoo_frayune_provider.py:call_api:{run_id}:{store}"],
        "tests": [],
        "commandLineContentSeparator": "<---SEND--->",
        "metadata": {"source": "frayune-evaluation", "authoritativeStore": "data/evaluation"},
    }
    target.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


class LangfuseExporter:
    """把评测事件翻译为 Langfuse trace 摄入 payload 并批量发送。

    使用 httpx 注入 transport 以便离线契约测试;真实 Langfuse 属待真实验收。
    """

    def __init__(self, public_key: str | None = None, secret_key: str | None = None, host: str | None = None, client=None):
        self.public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        self.secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY", "")
        self.host = (host or os.environ.get("LANGFUSE_HOST", "")).rstrip("/")
        self.client = client

    def to_ingest_payload(self, events: list[dict]) -> dict:
        traces = []
        for event in events:
            traces.append(
                {
                    "id": event.get("eventId") or event.get("sequence"),
                    "name": event.get("type", "eval.event"),
                    "timestamp": event.get("timestamp") or event.get("pushedAt"),
                    "metadata": {
                        "runId": event.get("runId"),
                        "trialId": event.get("trialId"),
                        "stepId": event.get("stepId"),
                        "origin": "evaluation",
                    },
                }
            )
        return {"batch": [{"type": "trace-create", "id": t["id"], "timestamp": t["timestamp"], "name": t["name"], "metadata": t["metadata"]} for t in traces]}

    def export(self, events: list[dict]) -> int:
        if not (self.public_key and self.secret_key and self.host):
            raise ValueError("Langfuse 未配置:需要 LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY/LANGFUSE_HOST")
        import httpx

        client = self.client or httpx.Client()
        response = client.post(
            f"{self.host}/api/public/ingestion",
            headers={
                "Authorization": "Basic "
                + base64.b64encode(f"{self.public_key}:{self.secret_key}".encode()).decode()
            },
            json=self.to_ingest_payload(events),
            timeout=15,
        )
        response.raise_for_status()
        return len(events)


def make_outbox_exporter(exporter: LangfuseExporter):
    """有界重试导出包装;失败信息写入返回值,不抛出(§9.3,TRACE-03)。"""

    def flush(outbox: Outbox) -> dict:
        def send(events):
            exporter.export(events)

        return outbox.flush(send, max_attempts=2)

    return flush
