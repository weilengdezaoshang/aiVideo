"""Deterministic tickets and scripted fake: no network/model dependency."""
import copy
import re
from .contracts import PlannerResult
from backend.agent.media.canvas_actions import is_cutout_request
from backend.agent.media.intent.negation import affirmative_text


def cutout_target(message: str, action: str | None = None) -> str | None:
    value = message.strip().rstrip("。！! ")
    if action == "cutout" or value in {"抠图", "抠出主体", "去背景"}:
        return ""
    if not is_cutout_request(value):
        return None
    match = re.fullmatch(r"(?:请|帮我|请帮我)?(?:把|将)?(.+?)(?:抠出来|抠出|抠下来|去掉背景)", value)
    return match[1].strip() if match else None


class RulePlanner:
    async def plan(self, context):
        request = context["request"]
        target = cutout_target(request["message"], request.get("action"))
        selected = request.get("selected_assets", [])
        if target is not None and len(selected) == 1:
            image = {"asset_id": selected[0], "media_type": "image"}
            plan = {"goal": request["message"], "steps": [
                {"id": "t1", "tool": "detect_subject", "parameters": {"image": image, "target_description": target}},
                {"id": "t2", "tool": "cutout", "parameters": {"image": image, "mask": "$t1.output"},
                 "depends_on": ["t1"], "placement": {"mode": "right", "anchor_id": request.get("anchor_id")}}]}
        elif request.get("tool") and request.get("action") == "tool":
            plan = {"goal": request["message"], "steps": [{"id": "t1", "tool": request["tool"],
                "parameters": request.get("parameters", {}), "placement": request.get("placement", {})}]}
        elif (single := single_step(context)) is not None:
            plan = single
        else:
            plan = {"goal": request["message"], "steps": [{"id": "t1", "kind": "ask_user",
                "parameters": {"question": "请明确要处理的图片、工具和生成要求。", "options": []}}]}
        return PlannerResult(plan=plan, invariants=context["invariants"], confidence=1,
            raw_output={"writer": "rule", "plan": plan})


class FakePlanner:
    """Script selection is in durable context; object recreation never resets its cursor."""
    def __init__(self, script):
        self.script = copy.deepcopy(script)
        self.calls = []

    async def plan(self, context):
        self.calls.append(context["invocation_key"])
        index = min(context["attempt"], len(self.script) - 1)
        return PlannerResult.model_validate(copy.deepcopy(self.script[index]))


def single_step(context):
    request = context["request"]
    message = affirmative_text(request["message"])
    if not re.search(r"生成|画一|编辑|修改|改图", message):
        return None
    video = bool(re.search(r"视频|动画|短片", message))
    duration = re.search(r"(\d+(?:\.\d+)?)\s*秒", message)
    if video and duration is None:
        return None  # Never guess missing video duration.
    selected = request.get("selected_assets", [])
    if len(selected) > 1:
        return None
    capability = ("image_to_video" if selected else "text_to_video") if video else ("image_edit" if selected else "text_to_image")
    tool = next((t for t in context.get("tools", []) if t["name"] == capability + "_preview" and t["available"]), None)
    if tool is None:
        return None
    properties = tool["parameters"]["properties"]
    params = {"prompt": request["message"], "count": 1}
    for name in ("width", "height", "fps"):
        if name in properties:
            values = properties[name].get("enum", [])
            if len(values) != 1:
                return None
            params[name] = values[0]
    if video:
        params["duration_seconds"] = float(duration[1])
        if params["duration_seconds"] not in properties["duration_seconds"].get("enum", []):
            return None
    if selected:
        params["image"] = {"asset_id": selected[0], "media_type": "image"}
    return {"goal": request["message"], "steps": [{"id": "t1", "tool": tool["name"], "parameters": params}]}
