"""Paginated span projections, with explicit linkage and field-level redaction."""

import base64
import json
import re
import uuid
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from sqlalchemy import select, or_, and_
from backend.core.orm import (
    CoreRun,
    CorePlan,
    CoreTask,
    CoreTaskAttempt,
    CoreEvent,
    CoreSessionMessage,
)
from backend.core.supervisor.orm import Invocation
from backend.agent.orm import HostCreditLedger
from .contracts import Settings
from backend.core.orm import CoreTaskDependency
from backend.core.execution.orm import TaskExecution, ExecutionAdmission
from backend.core.events.orm import EventReceipt, EventAttempt, EventAlert, EventQuarantine

SECTIONS = {
    "messages": CoreSessionMessage,
    "plans": CorePlan,
    "tasks": CoreTask,
    "calls": Invocation,
    "attempts": CoreTaskAttempt,
    "events": CoreEvent,
    "deliveries": EventAttempt,
    "alerts": EventAlert,
    "quarantines": EventQuarantine,
    "costs": HostCreditLedger,
    "children": CoreRun,
}
PRIVATE = re.compile(
    r"secret|password|credential|authorization|api.?key|access.?key|signature|signed.?url|token$|ticket|storage_key|locator|handle|candidates|_members",
    re.I,
)


def redact(value):
    if isinstance(value, dict):
        value = {
            key: (
                [
                    {k: item.get(k) for k in ("label", "confidence", "confidence_source")}
                    for item in items
                    if isinstance(item, dict)
                ]
                if key == "candidates" and isinstance(items, list)
                else items
            )
            for key, items in value.items()
        }
        return {
            key: (
                "[redacted]"
                if (PRIVATE.search(key) and key != "candidates")
                or (key == "mask" and not (isinstance(item, str) and item.startswith("$")))
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):

        def clean(match):
            try:
                url = urlsplit(match[0])
            except ValueError:
                return "[redacted URL]"
            host = url.netloc.rsplit("@", 1)[-1]
            return urlunsplit((url.scheme, host, url.path, "", ""))

        return re.sub(r"https?://[^\s\"<>]+", clean, value)
    return value


def serial(value):
    if isinstance(value, (datetime, uuid.UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, dict):
        return {k: serial(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [serial(v) for v in value]
    return value


def row_data(row):
    return redact(
        serial({column.name: getattr(row, column.name) for column in row.__table__.columns})
    )


def encode(row):
    return base64.urlsafe_b64encode(
        json.dumps([row.created_at.isoformat(), str(row.id)]).encode()
    ).decode()


def decode(cursor):
    try:
        if len(cursor) > 512:
            raise ValueError
        at, ident = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        stamp = datetime.fromisoformat(at)
        if stamp.tzinfo is None:
            raise ValueError
        return stamp, uuid.UUID(ident)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("invalid_cursor") from None


async def page(tx, run, section, cursor=None, *, settings=None):
    settings = settings or Settings()
    model = SECTIONS[section]
    if section == "events":
        query = select(CoreEvent).where(CoreEvent.run_id == run.id)
        if cursor:
            if len(cursor) > 512 or not cursor.startswith("seq:") or not cursor[4:].isdigit():
                raise ValueError("invalid_cursor")
            query = query.where(CoreEvent.run_sequence > int(cursor[4:]))
        query = query.order_by(CoreEvent.run_sequence)
    else:
        if section == "alerts":
            query = (
                select(model)
                .outerjoin(CoreEvent, CoreEvent.id == model.event_id)
                .where(
                    or_(CoreEvent.run_id == run.id, model.context["run_id"].astext == str(run.id))
                )
            )
        elif section == "deliveries":
            query = (
                select(model)
                .join(CoreEvent, CoreEvent.id == model.event_id)
                .where(CoreEvent.run_id == run.id)
            )
        else:
            column = (
                model.parent_run_id
                if section == "children"
                else model.core_run_id
                if section == "costs"
                else model.run_id
            )
            query = select(model).where(column == run.id)
        if cursor:
            at, ident = decode(cursor)
            query = query.where(
                or_(model.created_at > at, and_(model.created_at == at, model.id > ident))
            )
        query = query.order_by(model.created_at, model.id)
    rows = list(await tx.scalars(query.limit(settings.page_size + 1)))
    visible = rows[: settings.page_size]
    spans = []
    for row in visible:
        data = row_data(row)
        if section == "tasks":
            data["depends_on"] = [
                str(v)
                for v in await tx.scalars(
                    select(CoreTaskDependency.depends_on_id).where(
                        CoreTaskDependency.task_id == row.id
                    )
                )
            ]
            execution = await tx.get(TaskExecution, row.id)
            admission = await tx.scalar(
                select(ExecutionAdmission).where(ExecutionAdmission.task_id == row.id)
            )
            data["execution"] = row_data(execution) if execution else None
            data["admission"] = row_data(admission) if admission else None
        if section == "events":
            receipt = await tx.get(EventReceipt, row.id)
            data["processing"] = row_data(receipt) if receipt else None
        # Exclude invocation input catalogs/snapshots entirely: callers can inspect the pinned Plan, not execution credentials.
        if section == "calls":
            data.pop("context", None)
        spans.append(
            {
                "id": str(row.id),
                "kind": section,
                "run_id": str(row.id) if section == "children" else str(run.id),
                "parent_run_id": str(run.id)
                if section == "children"
                else str(run.parent_run_id)
                if run.parent_run_id
                else None,
                "task_id": str(getattr(row, "task_id", None) or getattr(row, "core_task_id", None))
                if getattr(row, "task_id", None) or getattr(row, "core_task_id", None)
                else str(row.id)
                if section == "tasks"
                else None,
                "trace_id": str(run.id),
                "at": row.created_at.isoformat(),
                "data": data,
                "links": {
                    k: str(getattr(row, k))
                    for k in ("event_id", "causation_id", "model_call_ref", "plan_id", "job_id")
                    if getattr(row, k, None)
                },
            }
        )
    return {
        "items": spans,
        "next": (
            ("seq:" + str(visible[-1].run_sequence)) if section == "events" else encode(visible[-1])
        )
        if len(rows) > settings.page_size
        else None,
    }


async def report(tx, run):
    sections = {key: await page(tx, run, key) for key in SECTIONS}
    from .invariants import check

    overview = row_data(run)
    return {
        "run": {
            k: overview[k]
            for k in (
                "id",
                "parent_run_id",
                "session_id",
                "status",
                "version",
                "error",
                "credits_limit",
                "credits_reserved",
                "credits_spent",
                "tokens_limit",
                "tokens_used",
                "thought_rounds_used",
                "created_at",
            )
        },
        "sections": sections,
        "invariants": await check(tx, run.id, settled=False),
    }
