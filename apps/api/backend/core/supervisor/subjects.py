"""Subject choice is program policy; only opaque option IDs cross the answer API."""
from backend.core.orm import CoreTaskDependency, CorePlan
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


async def handle_subjects(tx, run, tasks, cause, settings):
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
            await transition(tx, "task", cutout.id, cutout.version, "queued", causation_id=cause)
            continue
        candidates = result["candidates"]
        await message(tx, run, "识别到：" + "、".join(c["label"] for c in candidates) if candidates else "未识别到可抠的主体。",
            "subjects:" + str(detection.id))
        kind, indices = decision(result, detection.parameters.get("target_description", ""), run.mode, settings.confidence_threshold)
        if kind == "none":
            await transition(tx, "task", cutout.id, cutout.version, "cancelled", causation_id=cause)
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
            await transition(tx, "run", run.id, run.version, "paused", reason="waiting_user", causation_id=cause)
            await message(tx, run, waiting["question"], "ask:" + str(question.id), details={"options": options})
            return True
        selected = [candidates[i]["mask"] for i in indices]
        detection.waiting = {"selected_output": selected[0], "selected_handles": [m["handle"] for m in selected]}
        await transition(tx, "task", cutout.id, cutout.version, "queued", causation_id=cause)
    return False
