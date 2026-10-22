"""Operational windows are DB gauges; IDs never become metric labels."""

from datetime import timedelta
import math
from sqlalchemy import select, func, case, update, or_
from backend.core.orm import CoreRun, CoreTask, CoreEvent
from backend.core.events.integrity import completion_proof
from backend.core.supervisor.orm import Invocation
from backend.core.events.orm import EventReceipt, EventAlert, EventQuarantine
from backend.core.events.store import alert, db_now
from backend.core.tools.orm import ToolDefinitionRecord
from backend.agent.orm import HostCreditLedger
from .contracts import Settings

FAILURES = (
    "retryable",
    "param_invalid",
    "no_subject",
    "blocked_by_model",
    "quota",
    "provider_down",
    "unknown",
)


async def snapshot(tx, settings=None):
    settings = settings or Settings()
    stamp = await db_now(tx)
    since = stamp - timedelta(seconds=settings.window_seconds)
    runs = dict(
        (
            await tx.execute(
                select(CoreRun.status, func.count())
                .where(CoreRun.finished_at >= since)
                .group_by(CoreRun.status)
            )
        ).all()
    )
    names = list(
        await tx.scalars(
            select(ToolDefinitionRecord.name)
            .distinct()
            .order_by(ToolDefinitionRecord.name)
            .limit(settings.max_tool_labels)
        )
    )
    tool = case((CoreTask.tool_ref.in_(names), CoreTask.tool_ref), else_="other")
    failure = case(
        (CoreTask.status == "blocked_by_model", "blocked_by_model"),
        (CoreTask.error["code"].astext.in_(FAILURES), CoreTask.error["code"].astext),
        else_="unknown",
    )
    tools = (
        await tx.execute(
            select(tool, CoreTask.status, failure, func.count())
            .where(CoreTask.kind == "call_tool", CoreTask.finished_at >= since)
            .group_by(tool, CoreTask.status, failure)
        )
    ).all()
    verdict = Invocation.review["verdict"].astext
    reviews = dict(
        (
            await tx.execute(
                select(verdict, func.count())
                .where(
                    verdict.is_not(None),
                    func.coalesce(Invocation.reviewed_at, Invocation.created_at) >= since,
                )
                .group_by(verdict)
            )
        ).all()
    )
    backlog = await tx.scalar(
        select(func.count())
        .select_from(CoreEvent)
        .outerjoin(EventReceipt, EventReceipt.event_id == CoreEvent.id)
        .where(
            or_(
                EventReceipt.event_id.is_(None),
                EventReceipt.status.in_(["pending", "retry", "running", "waiting_handler"]),
            ),
            ~completion_proof(CoreEvent.id, CoreEvent.run_id, CoreEvent.run_sequence),
        )
    )
    dead = await tx.scalar(
        select(func.count()).select_from(EventReceipt).where(EventReceipt.status == "dead")
    )
    quarantines = await tx.scalar(
        select(func.count())
        .select_from(EventQuarantine)
        .where(EventQuarantine.resolved_at.is_(None))
    )
    dead += quarantines
    refunds = await tx.scalar(
        select(func.coalesce(func.sum(HostCreditLedger.refunded), 0)).where(
            HostCreditLedger.core_run_id.is_not(None), HostCreditLedger.updated_at >= since
        )
    )
    unknown = await tx.scalar(
        select(func.count())
        .select_from(EventAlert)
        .where(EventAlert.kind == "unknown_tool_failure", EventAlert.last_seen_at >= since)
    )
    return {
        "runs": runs,
        "tools": tools,
        "reviews": reviews,
        "backlog": backlog,
        "dead": dead,
        "refunds": refunds,
        "unknown": unknown,
        "window_seconds": settings.window_seconds,
    }


def ratio(a, b):
    return a / b if b else float("nan")


async def render(factory):
    async with factory() as tx:
        data = await snapshot(tx)
    lines = []

    def sample(name, value, labels=None):
        suffix = (
            "{"
            + ",".join(
                f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34)).replace(chr(10), chr(92) + "n")}"'
                for k, v in labels.items()
            )
            + "}"
            if labels
            else ""
        )
        number = "NaN" if isinstance(value, float) and math.isnan(value) else str(value)
        lines.append(f"aivideo_core_{name}{suffix} {number}")

    sample(
        "run_success_rate",
        ratio(
            data["runs"].get("completed", 0),
            data["runs"].get("completed", 0) + data["runs"].get("failed", 0),
        ),
    )
    for state in ("completed", "failed", "cancelled"):
        sample("run_outcomes", data["runs"].get(state, 0), {"status": state})
    totals, failures = {}, {}
    for tool, status, failure, count in data["tools"]:
        if status in {"succeeded", "failed", "blocked_by_model"}:
            totals[tool] = totals.get(tool, 0) + count
        if status in {"failed", "blocked_by_model"}:
            failures[tool, failure] = failures.get((tool, failure), 0) + count
    for tool, total in totals.items():
        sample("tool_outcomes", total, {"tool": tool})
        for failure in FAILURES:
            count = failures.get((tool, failure), 0)
            sample("tool_failures", count, {"tool": tool, "failure": failure})
            sample("tool_failure_rate", ratio(count, total), {"tool": tool, "failure": failure})
    sample(
        "review_rejection_rate",
        ratio(data["reviews"].get("reject", 0), sum(data["reviews"].values())),
    )
    for name in ("backlog", "dead", "refunds", "unknown", "window_seconds"):
        sample(
            {
                "backlog": "event_backlog",
                "dead": "dead_letters",
                "refunds": "refund_credits",
                "unknown": "unknown_errors",
            }.get(name, name),
            data[name],
        )
    names = sorted({line.split("{", 1)[0].split(" ", 1)[0] for line in lines})
    return [*(f"# TYPE {name} gauge" for name in names), *lines]


async def monitor(factory, settings=None):
    settings = settings or Settings()
    async with factory.begin() as tx:
        data = await snapshot(tx, settings)
        run_total = data["runs"].get("completed", 0) + data["runs"].get("failed", 0)
        conditions = {
            "run_failure_rate": run_total >= settings.min_samples
            and ratio(data["runs"].get("failed", 0), run_total) >= settings.run_failure_rate,
            "review_rejection_rate": sum(data["reviews"].values()) >= settings.min_samples
            and ratio(data["reviews"].get("reject", 0), sum(data["reviews"].values()))
            >= settings.review_rejection_rate,
            "event_backlog": data["backlog"] >= settings.backlog_count,
            "dead_letters": data["dead"] > 0,
            "refund_credits": data["refunds"] > settings.refund_credits,
            "unknown_errors": data["unknown"] > 0,
        }
        totals, failed = {}, {}
        for tool, status, _, count in data["tools"]:
            if status in {"succeeded", "failed", "blocked_by_model"}:
                totals[tool] = totals.get(tool, 0) + count
            if status in {"failed", "blocked_by_model"}:
                failed[tool] = failed.get(tool, 0) + count
        conditions["tool_failure_rate"] = any(
            n >= settings.min_samples and ratio(failed.get(t, 0), n) >= settings.tool_failure_rate
            for t, n in totals.items()
        )
        stamp = await db_now(tx)
        for kind, active in conditions.items():
            key = "diagnostics:" + kind
            if active:
                old = await tx.scalar(select(EventAlert).where(EventAlert.dedupe_key == key))
                if (
                    old is None
                    or old.resolved_at
                    or (stamp - old.last_seen_at).total_seconds() >= settings.poll_seconds
                ):
                    await alert(
                        tx,
                        kind,
                        None,
                        key,
                        context={
                            "window_seconds": settings.window_seconds,
                            "run_counts": data["runs"],
                            "reviews": data["reviews"],
                            "backlog": data["backlog"],
                            "dead": data["dead"],
                            "refund_credits": int(data["refunds"]),
                            "unknown_errors": data["unknown"],
                        },
                    )
            else:
                await tx.execute(
                    update(EventAlert)
                    .where(EventAlert.dedupe_key == key, EventAlert.resolved_at.is_(None))
                    .values(resolved_at=stamp)
                )
