import hashlib
import json
import logging
import math
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

from .common import now


def cost(model):
    for pattern, value in [
        (r"cogview-4", 0.06),
        (r"FLUX\.1-schnell", 0.01),
        (r"FLUX\.1-dev", 0.25),
        (r"gpt-image", 0.25),
    ]:
        if re.search(pattern, model, re.IGNORECASE):
            return value
    return None


def classify(message):
    for pattern, category in [
        ("已取消", "cancelled"),
        ("超时|timeout", "timeout"),
        ("401|403|未授权|API Key|Authorization", "auth"),
        ("429|限流|rate.?limit", "rate_limit"),
        ("400|参数|invalid|不存在", "invalid_request"),
        ("HTTP 5|500|502|503|服务器", "server_error"),
        ("fetch|network|ECONN|ENOTFOUND|下载失败|connect", "network"),
    ]:
        if re.search(pattern, message, re.IGNORECASE):
            return category
    return "unknown"


def percentile(values, q):
    return sorted(values)[max(0, math.ceil(q * len(values)) - 1)] if values else 0


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Traces:
    def __init__(self, file):
        self.file = file

    def append(self, record):
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with self.file.open("a") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logging.exception("追踪写入失败")

    def terminal(self, job, provider):
        p = job["params"]
        status = (
            "completed"
            if job["status"] == "completed"
            else "cancelled"
            if "已取消" in job.get("error", "")
            else "failed"
        )
        prompt = p.get("sourcePrompt") or p["prompt"]
        self.append(
            dict(
                ts=now(),
                kind="trace",
                jobId=job["id"],
                clientRef=job.get("clientRef"),
                provider=provider.name,
                model=p["model"],
                origin="user",
                translated=bool(p.get("sourcePrompt")),
                promptHash=hashlib.sha256(prompt.encode()).hexdigest()[:12],
                promptChars=len(prompt),
                seed=p["seed"],
                width=p["width"],
                height=p["height"],
                kind_of_media=p["kind"],
                status=status,
                queueMs=max(
                    0,
                    (
                        timestamp(job.get("startedAt", job["finishedAt"]))
                        - timestamp(job["createdAt"])
                    ).total_seconds()
                    * 1000,
                ),
                latencyMs=max(
                    0,
                    (
                        timestamp(job["finishedAt"])
                        - timestamp(job.get("startedAt", job["finishedAt"]))
                    ).total_seconds()
                    * 1000,
                ),
                imageId=job["images"][0]["id"] if job["images"] else None,
                errorCategory=classify(job.get("error", "")) if status == "failed" else None,
            )
        )

    def aggregate(self, days=7):
        records = []
        since = datetime.now(UTC) - timedelta(days=days)
        if self.file.exists():
            for line in self.file.read_text().splitlines():
                try:
                    item = json.loads(line)
                    if timestamp(item["ts"]) >= since:
                        records.append(item)
                except (ValueError, KeyError, TypeError):
                    continue
        traces = [r for r in records if r.get("kind") == "trace"]
        done = [r for r in traces if r["status"] == "completed"]
        feedback = [r for r in records if r.get("kind") == "feedback"]
        deleted = Counter(r.get("imageId") for r in feedback)

        def stats(items):
            counts = Counter(r["status"] for r in items)
            return dict(
                total=len(items),
                completed=counts["completed"],
                failed=counts["failed"],
                cancelled=counts["cancelled"],
                successRate=counts["completed"] / len(items) if items else 0,
            )

        models = []
        for model in sorted({r["model"] for r in traces}):
            items = [r for r in traces if r["model"] == model]
            completed = [r for r in items if r["status"] == "completed"]
            removed = sum(deleted[r.get("imageId")] for r in completed)
            models.append(
                dict(
                    model=model,
                    **stats(items),
                    p50LatencyMs=percentile([r["latencyMs"] for r in completed], 0.5),
                    p95LatencyMs=percentile([r["latencyMs"] for r in completed], 0.95),
                    costCny=(cost(model) or 0) * len(completed),
                    deleted=removed,
                    keptRate=1 - min(1, removed / len(completed)) if completed else 1,
                )
            )
        errors = Counter(
            r.get("errorCategory") or "unknown" for r in traces if r["status"] == "failed"
        )
        return dict(
            windowDays=days,
            generations=stats(traces),
            latency=dict(
                p50Ms=percentile([r["latencyMs"] for r in done], 0.5),
                p95Ms=percentile([r["latencyMs"] for r in done], 0.95),
            ),
            queue=dict(p50Ms=percentile([r["queueMs"] for r in done], 0.5)),
            cost=dict(
                totalCny=sum(cost(r["model"]) or 0 for r in done),
                knownCount=sum(cost(r["model"]) is not None for r in done),
            ),
            byModel=sorted(models, key=lambda x: x["total"], reverse=True),
            errors=[dict(category=k, count=v) for k, v in errors.most_common()],
            feedback=dict(deleted=len(feedback)),
        )
