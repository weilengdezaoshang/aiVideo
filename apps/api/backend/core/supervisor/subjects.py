"""Subject choice is program policy; only opaque option IDs cross the answer API."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.core.orm import CoreEvent, CoreTaskDependency, CorePlan
from backend.core.contracts import TaskInput
from backend.core.store import create_task, transition
from .service import message


def decision(result, target, mode, threshold):
    candidates = result["candidates"]
    if not candidates:
        return "none", []
    # Exact normalized labels only. Unsupported semantic matching must ask, never guess.
    matched = [i for i, c in enumerate(candidates) if target and target in c["label"]]
    eligible = matched if target else list(range(len(candidates)))
    if mode != "plan" and len(eligible) == 1:
        candidate = candidates[eligible[0]]
        if candidate["confidence"] is not None and candidate["confidence"] >= threshold:
            return "auto", eligible
    return "ask", []


async def handle_subjects(tx, run, tasks, cause, settings, *, batch: bool = False):
    for detection in tasks:
        if detection.tool_ref != "detect_subject" or detection.status != "succeeded":
            continue
        result = detection.output
        cutout = next((t for t in tasks if t.tool_ref == "cutout" and t.parameters.get("mask") == f"${detection.task_key}.output"), None)
        if cutout is None or cutout.status not in {"validated", "queued"}:
            continue
        if cutout.status == "queued":
            continue
        state = detection.waiting or {}
        if state.get("selected_output"):
            if not batch:
                await transition(tx, "task", cutout.id, cutout.version, "queued", causation_id=cause)
            continue
        candidates = result["candidates"]
        await message(tx, run, "识别到：" + "、".join(c["label"] for c in candidates) if candidates else "未识别到可抠的主体。",
            "subjects:" + str(detection.id))
        kind, indices = decision(result, detection.parameters.get("target_description", ""), run.mode, settings.confidence_threshold)
        if kind == "none":
            cutout.error = {"code": "no_subject"}
            await transition(tx, "task", cutout.id, cutout.version, "cancelled", causation_id=cause, reason="no_subject")
            continue
        question = next((t for t in tasks if t.kind == "ask_user" and (t.waiting or {}).get("detection_id") == str(detection.id)), None)
        if question:
            if question.status != "succeeded":
                return True
            indices = question.waiting["mapping"][question.waiting["answer"]]
            if run.status == "paused":
                await transition(tx, "run", run.id, run.version, "running", causation_id=cause)
        elif kind == "ask":
            plan = await tx.get(CorePlan, cutout.plan_id)
            options = [{"id": str(i), "label": c["label"]} for i, c in enumerate(candidates)] + [{"id": "all", "label": "全部"}]
            waiting = {"detection_id": str(detection.id), "question": "请选择要抠出的主体。", "options": options,
                "mapping": {**{str(i): [i] for i in range(len(candidates))}, "all": list(range(len(candidates)))}}
            number = max(int(t.task_key[1:]) for t in tasks) + 1
            question = await create_task(tx, TaskInput(run_id=run.id, plan_id=plan.id, plan_version=plan.version,
                task_key="t" + str(number), kind="ask_user", parameters={"question": waiting["question"], "options": options},
                idempotency_key="subject-choice:" + str(detection.id), scope=run.scope))
            question.waiting = waiting
            # Program-written interaction tickets also pass review; media plan is unchanged.
            from .reviewer import review
            from .contracts import Plan
            from backend.core.tools.store import load_catalog
            catalog = await load_catalog(tx, run.version_lock["tool_catalog"]["version"])
            interaction = Plan.model_validate({**plan.proposal, "steps": [*plan.proposal["steps"], {"id": question.task_key, "kind": "ask_user"}]})
            check = review(interaction, run, catalog, run.credits_limit)
            if check["verdict"] != "pass":
                raise ValueError("generated interaction failed review")
            question.waiting = {**waiting, "review": check}
            question.review_ref = "subject-choice:" + str(detection.id)
            await transition(tx, "task", question.id, question.version, "validated", causation_id=cause)
            await transition(tx, "task", question.id, question.version, "waiting_user", causation_id=cause)
            tx.add(CoreTaskDependency(task_id=question.id, depends_on_id=detection.id, run_id=run.id, plan_id=plan.id))
            tx.add(CoreTaskDependency(task_id=cutout.id, depends_on_id=question.id, run_id=run.id, plan_id=plan.id))
            if not batch:
                await transition(tx, "run", run.id, run.version, "paused", reason="waiting_user", causation_id=cause)
            await message(tx, run, waiting["question"], "ask:" + str(question.id), details={"options": options})
            if not batch:
                return True
            continue
        selected = [candidates[i]["mask"] for i in indices]
        detection.waiting = {"selected_output": selected[0], "selected_handles": [m["handle"] for m in selected]}
        if not batch:
            await transition(tx, "task", cutout.id, cutout.version, "queued", causation_id=cause)
    return False


class SubjectSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_SUBJECT_", frozen=True, extra="forbid")
    choice_ttl_seconds: float = Field(default=300, gt=0)
    scan_limit: int = Field(default=20, gt=0)


def _normalize(text: str) -> str:
    return "".join(str(text or "").split()).lower()


def match_waiting_option(labels: list[dict], text: str) -> str | None:
    """Match a free-form chat message onto a waiting option; no model involved.

    The message must name the option (or its label) without ambiguity; a
    miss stays a regular interruption and never guesses.
    """
    needle = _normalize(text)
    if not needle:
        return None
    exact = [option for option in labels if _normalize(option.get("label") or "") == needle]
    if len(exact) == 1:
        return exact[0].get("id")
    contained = [option for option in labels
                 if _normalize(option.get("label") or "") and _normalize(option["label"]) in needle]
    if contained:
        return max(contained, key=lambda option: len(_normalize(option.get("label") or ""))).get("id")
    covering = [option for option in labels
                if _normalize(option.get("label") or "") and needle in _normalize(option["label"])]
    if len(covering) == 1:
        return covering[0].get("id")
    return None


async def try_message_answer(tx, run, text: str) -> bool:
    """A waiting subject-choice can be answered by plain chat (e.g. 抠杯子)."""
    from sqlalchemy import select as _select

    from backend.core.orm import CoreTask
    question = await tx.scalar(_select(CoreTask).where(CoreTask.run_id == run.id,
        CoreTask.status == "waiting_user", CoreTask.kind == "ask_user",
        CoreTask.idempotency_key.like("subject-choice:%")).order_by(CoreTask.created_at).limit(1))
    if question is None or run.status != "paused" or run.pause_reason != "waiting_user":
        return False
    waiting = question.waiting or {}
    option_id = match_waiting_option(waiting.get("options") or [], text)
    if option_id is None:
        return False
    from .service import answer
    try:
        await answer(tx, task_id=question.id, option_id=option_id,
            request_key="subject-chat:" + digest_message(text), workspace_id=run.workspace_id,
            user_id=run.user_id)
    except ValueError:
        return False
    return True


def digest_message(text: str) -> str:
    import hashlib
    return hashlib.sha256(("subject-chat:" + text).encode()).hexdigest()[:64]


async def expire_stale_choices(factory, settings: SubjectSettings | None = None) -> int:
    """Cancel waiting subject choices past their TTL; nothing was reserved."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select as _select

    from backend.core.orm import CoreRun, CoreTask
    from backend.core.store import transition
    from .engine import stop
    from .service import message

    settings = settings or SubjectSettings()
    horizon = datetime.now(timezone.utc) - timedelta(seconds=settings.choice_ttl_seconds)
    expired = 0
    async with factory.begin() as tx:
        rows = list((await tx.scalars(_select(CoreTask).where(CoreTask.status == "waiting_user",
            CoreTask.kind == "ask_user", CoreTask.idempotency_key.like("subject-choice:%"),
            CoreTask.updated_at < horizon).limit(settings.scan_limit)
            .with_for_update(skip_locked=True))))
        for question in rows:
            run = await tx.scalar(_select(CoreRun).where(CoreRun.id == question.run_id)
                .with_for_update(skip_locked=True))
            if run is None or run.status in ("completed", "failed", "cancelled"):
                continue
            cause = await tx.scalar(_select(CoreEvent.id).where(CoreEvent.run_id == run.id)
                .order_by(CoreEvent.run_sequence.desc()).limit(1))
            await transition(tx, "task", question.id, question.version, "cancelled", causation_id=cause)
            await message(tx, run, "超时未选择主体，已自动取消本次抠图（未扣费）。",
                "subject-ttl:" + str(question.id))
            await stop(tx, run, "cancelled", "超时未选择主体，本次运行已结束（未扣费）。",
                "subject-ttl:" + str(run.id))
            expired += 1
    return expired
