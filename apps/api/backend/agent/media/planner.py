"""Host Intent and template Planner. Unknown paths clarify or fail; video never becomes image."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from .catalog import Catalog, HostError
from .canvas_actions import is_cutout_request
from .contracts import MediaPlan, MediaStep, MultiPlan


@dataclass(frozen=True)
class Violation:
    code: str
    stepId: str | None = None


def validate_plan(plan: MediaPlan, catalog: Catalog) -> list[Violation]:
    errors = []
    by_id = {step.id: step for step in plan.steps}
    if len(by_id) != len(plan.steps):
        return [Violation("plan_duplicate_id")]
    for step in plan.steps:
        if step.fanOut != len(step.targetNodes) or (step.variants and len(step.variants) != step.fanOut):
            errors.append(Violation("plan_fanout_node_mismatch", step.id))
        if len(set(step.targetNodes)) != len(step.targetNodes):
            errors.append(Violation("plan_node_contention", step.id))
        if step.tier != plan.tier:
            errors.append(Violation("plan_tier_conflict", step.id))
        if (len(set(step.dependsOn)) != len(step.dependsOn) or
                any(dep == step.id or dep not in by_id for dep in step.dependsOn)):
            errors.append(Violation("plan_bad_dependency", step.id))
        try:
            catalog.resolve(step.capabilityId, step.tier)
        except HostError as exc:
            errors.append(Violation(exc.code, step.id))
    if errors:
        return errors
    ancestors, remaining = {}, list(plan.steps)
    while remaining:
        ready = next((step for step in remaining if all(dep in ancestors for dep in step.dependsOn)), None)
        if ready is None:
            return [Violation("plan_cycle")]
        ancestors[ready.id] = set(ready.dependsOn)
        for dep in ready.dependsOn:
            ancestors[ready.id].update(ancestors[dep])
        remaining.remove(ready)
    for step in plan.steps:
        cap = catalog.resolve(step.capabilityId, step.tier)
        if step.capabilityId == "frames_to_video" and not 2 <= len(step.inputFrom) <= cap.maxRefImages:
            errors.append(Violation("plan_frame_count_invalid", step.id))
        if step.inputFrom and any(catalog.resolve(by_id[item.stepId].capabilityId, plan.tier).outputType != "image"
                                 for item in step.inputFrom if item.stepId in by_id):
            errors.append(Violation("plan_input_type_invalid", step.id))
        if step.capabilityId == "text_to_image" and step.inputFrom:
            errors.append(Violation("plan_unexpected_reference", step.id))
        if any(item.stepId not in ancestors[step.id] or item.index >= by_id[item.stepId].fanOut
               for item in step.inputFrom):
            errors.append(Violation("plan_input_not_dependent", step.id))
        if step.parallelGroup:
            if any(other.parallelGroup == step.parallelGroup and set(other.dependsOn) != set(step.dependsOn)
                   for other in plan.steps):
                errors.append(Violation("plan_parallel_group_inconsistent", step.id))
        for other in plan.steps:
            if (other.id != step.id and set(step.targetNodes) & set(other.targetNodes)
                    and other.id not in ancestors[step.id] and step.id not in ancestors[other.id]):
                errors.append(Violation("plan_node_contention", step.id))
    terminal = [step for step in plan.steps if not any(step.id in other.dependsOn for other in plan.steps)]
    if plan.goalType == "video" and any(catalog.resolve(step.capabilityId, step.tier).outputType != "video"
                                       for step in terminal):
        errors.append(Violation("plan_video_degraded_to_image"))
    if plan.tier == "final" and not plan.sourceAssetIds:
        errors.append(Violation("plan_missing_source_assets"))
    return errors


def plan_intent(message: str, references: list[str], catalog: Catalog, *, tier="preview", goal_type=None) -> MediaPlan:
    """Only natural language and owned asset identifiers cross the Host boundary."""
    if re.search(r"^(?:分析|检查|解释|画布上|帮我梳理)", message) and "；补充需求：" not in message:
        raise HostError("intent_clarify", "希望基于这些素材创作什么图片或视频？", status=422)
    if re.fullmatch(r"继续|重试|再来", message.strip()):
        raise HostError("intent_clarify", "请明确新的创作目标，或点击失败步骤的重试按钮", status=422)
    cleaned = re.sub(r"(?:不要|不需要|无需|不做)\s*(?:视频|动画|短片)", "", message)
    duration_match = re.search(r"(\d+(?:\.\d+)?|十二|十一|十|九|八|七|六|五|四|三|二|一)\s*秒", cleaned)
    chinese = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
    requested_duration = (chinese[duration_match[1]] if duration_match[1] in chinese else float(duration_match[1])) if duration_match else None
    image_only = bool(re.search(r"只要.*(?:图片|海报|图)|不要视频|不需要视频", message))
    is_video = (goal_type == "video" if goal_type else not image_only and
        bool(re.search(r"视频|动画|短片|video|animation", cleaned, re.I) or (duration_match and re.search(r"广告|镜头|片", cleaned))))
    ignore = bool(re.search(r"不要参考|忽略参考|纯文生", message))
    refs = [] if ignore else references
    if len(refs) > 1:
        raise HostError("intent_clarify", "请选择一张主参考图后继续", status=422)
    if is_cutout_request(message):
        raise HostError("intent_clarify", "请在画布检查主体选区并确认抠图", status=422)
    if re.search(r"局部|音频|配音|参考视频", message):
        raise HostError("capability_unavailable", "当前部署未开通此编辑或音频能力", status=422)
    if re.search(r"动起来|好看一点|优化一下", message) and not re.search(r"视频|动画|图片|海报", message):
        raise HostError("intent_clarify", "请明确希望生成图片还是视频", status=422)
    def new_node():
        return "host_" + uuid.uuid4().hex
    if is_video and not refs and not ignore and re.search(r"图生|参考图|这张图|选中.*(?:图片|素材)|image.to.video", message, re.I):
        raise HostError("intent_clarify", "请上传并选中一张可用参考图", status=422)
    if re.fullmatch(r"生成正片|确认正片|正片", message.strip()):
        raise HostError("intent_clarify", "请点击对应样片的生成正片按钮", status=422)
    if is_video and refs:
        # Direct I2V is used only when explicitly available and requested.
        direct = catalog.entries.get("image_to_video")
        if re.search(r"直接图生视频", message) and direct and direct.enabled:
            steps = [MediaStep(id="video", title="生成视频样片", capabilityId="image_to_video",
                dependsOn=[], targetNodes=[new_node()], tier=tier, prompt=message)]
        else:
            frames = [MediaStep(id=f"frame_{index}", title=f"生成关键帧 {index + 1}",
                capabilityId="image_edit", dependsOn=[], targetNodes=[new_node()], tier=tier,
                prompt=f"{message}；连续动作关键帧 {index + 1}/4，保持主体和场景一致",
                parallelGroup="keyframes") for index in range(4)]
            steps = [*frames, MediaStep(id="video", title="插帧合成视频样片", capabilityId="frames_to_video",
                dependsOn=[step.id for step in frames], targetNodes=[new_node()], tier=tier, prompt=message,
                inputFrom=[{"stepId": step.id} for step in frames], onFailure="fail_run")]
    elif is_video:
        steps = [MediaStep(id="video", title="生成视频样片", capabilityId="text_to_video", dependsOn=[],
            targetNodes=[new_node()], tier=tier, prompt=message)]
    elif re.search(r"四(?:个)?方向|前后左右|四(?:个)?视角", message):
        steps = [MediaStep(id="directions", title="生成四方向画面", capabilityId="image_edit" if refs else "text_to_image",
            dependsOn=[], targetNodes=[new_node() for _ in range(4)], tier=tier, prompt=message,
            parallelGroup="directions", fanOut=4,
            variants=[f"{message}；视角：{direction}" for direction in ("正面", "背面", "左侧", "右侧")],
            onFailure="keep_partial")]
    else:
        steps = [MediaStep(id="image", title="生成画面", capabilityId="image_edit" if refs else "text_to_image",
            dependsOn=[], targetNodes=[new_node()], tier=tier, prompt=message)]
    plan = MediaPlan(goal=message, goalType="video" if is_video else "image", tier=tier,
        steps=steps, sourceAssetIds=refs, reviewMode=catalog.config.reviewMode,
        maxConcurrency=min(catalog.config.maxConcurrency, *(catalog.resolve(step.capabilityId, tier).binding.maxConcurrency for step in steps)))
    if re.search(r"(?:4k|8k)\s*(?:视频|图片|海报|分辨率)|(?:分辨率|画质|清晰度)\s*(?:4k|8k)", message, re.I):
        raise HostError("spec_not_allowed", "当前部署未开放该高分辨率规格，请调整清晰度", status=422)
    ratio = re.search(r"(?<!\d)(16:9|9:16|1:1|4:3|3:4)(?!\d)", message)
    if ratio:
        width, height = (int(value) for value in ratio[1].split(':'))
        spec = catalog.resolve(steps[-1].capabilityId, tier).tiers[tier]
        if abs(width / height - spec.width / spec.height) > 0.01:
            raise HostError("spec_not_allowed", "当前能力规格不支持该画幅，请调整比例", status=422)
    if is_video and requested_duration is not None:
        terminal = steps[-1]
        allowed = catalog.resolve(terminal.capabilityId, tier).tiers[tier].durationSec
        if abs(requested_duration - allowed) > 0.01:
            raise HostError("spec_not_allowed", f"当前部署支持 {allowed:g} 秒视频，请调整时长后继续", status=422)
    violations = validate_plan(plan, catalog)
    if violations:
        codes = ",".join(item.code for item in violations)
        raise HostError(violations[0].code, f"无法执行本次创作计划：{codes}", status=422)
    return plan


def plan_from_decision(decision, references: list[str], catalog: Catalog, *, tier='preview') -> MediaPlan:
    """只按已锁定 L2 填模板；不得二次识别或用槽位改写目标。"""
    if decision.clarify or decision.certainty != 'strong':
        raise HostError('intent_clarify', '请先确认创作目标', status=422)
    l2, slots = decision.l2, decision.slots
    if decision.path and decision.path != l2:
        raise HostError('intent_invalid', '意图与执行路径不一致', status=422)
    message = slots.get('prompt', '')
    if l2 not in {'text_to_image', 'image_edit', 'text_to_video', 'image_to_multiframe', 'image_to_video'}:
        raise HostError('capability_unavailable', '当前仅支持图片或视频创作', status=422)
    if l2 == 'text_to_video' and not references and re.search(r'这张图|图生|参考图|选中.*(?:图片|素材)|this image|image.to.video', message, re.I):
        raise HostError('intent_clarify', '请上传并选中一张可用参考图后重新发送', status=422)
    needs_image = l2 in {'image_edit', 'image_to_multiframe', 'image_to_video'}
    refs = list(references) if needs_image else []
    if needs_image and len(refs) != 1:
        raise HostError('intent_clarify', '请选择一张可用主参考图', status=422)
    def node():
        return 'host_' + uuid.uuid4().hex
    if l2 == 'image_to_multiframe':
        frames = [MediaStep(id=f'frame_{index}', title=f'生成关键帧 {index + 1}', capabilityId='image_edit',
            dependsOn=[], targetNodes=[node()], tier=tier,
            prompt=f'{message}；连续动作关键帧 {index + 1}/4，保持主体和场景一致',
            parallelGroup='keyframes', onFailure='fail_run') for index in range(4)]
        steps = [*frames, MediaStep(id='video', title='插帧合成视频样片', capabilityId='frames_to_video',
            dependsOn=[step.id for step in frames], inputFrom=[{'stepId': step.id} for step in frames],
            targetNodes=[node()], tier=tier, prompt=message, onFailure='fail_run')]
    else:
        steps = [MediaStep(id='video' if l2 in {'text_to_video', 'image_to_video'} else 'image',
            title='生成视频样片' if l2 in {'text_to_video', 'image_to_video'} else '生成画面',
            capabilityId=l2, dependsOn=[], targetNodes=[node()], tier=tier, prompt=message, onFailure='fail_run')]
    terminal = catalog.resolve(steps[-1].capabilityId, tier).tiers[tier]
    if slots.get('duration_sec') is not None and l2 in {'text_to_video', 'image_to_multiframe', 'image_to_video'}:
        if abs(slots['duration_sec'] - terminal.durationSec) > 0.01:
            raise HostError('spec_not_allowed', f'当前部署支持 {terminal.durationSec:g} 秒视频，请调整时长后继续', status=422)
    if slots.get('ratio'):
        width, height = map(int, slots['ratio'].split(':'))
        if abs(width / height - terminal.width / terminal.height) > 0.01:
            raise HostError('spec_not_allowed', '当前能力规格不支持该画幅，请调整比例', status=422)
    if re.search(r'(?<![A-Za-z0-9])[48]k(?![A-Za-z0-9])', message, re.I):
        raise HostError('spec_not_allowed', '当前部署未开放该高分辨率规格', status=422)
    plan = MediaPlan(goal=message, goalType='video' if l2 in {'text_to_video', 'image_to_multiframe', 'image_to_video'} else 'image',
        tier=tier, steps=steps, sourceAssetIds=refs, reviewMode=catalog.config.reviewMode,
        maxConcurrency=min(catalog.config.maxConcurrency, *(catalog.resolve(step.capabilityId, tier).binding.maxConcurrency for step in steps)))
    violations = validate_plan(plan, catalog)
    if violations:
        raise HostError(violations[0].code, '无法执行本次创作计划：' + ','.join(item.code for item in violations), status=422)
    return plan


def plan_from_multi(decision, references: list[str], catalog: Catalog, *, tier='preview') -> MediaPlan:
    """逐个套用单步模板，以前步末端产物绑定后步所有根节点。"""
    from .intent.types import IntentDecision
    steps = decision.steps or []
    if len(steps) <= 1:
        return plan_from_decision(decision, references, catalog, tier=tier)
    # 重新校验模型，拒绝绕过类型校验的 model_copy 或内部调用。
    IntentDecision.model_validate(decision.model_dump())
    combined, previous = [], []
    source_ids = []
    for index, intent_step in enumerate(steps):
        single = IntentDecision(certainty='strong', l2=intent_step.l2, path=intent_step.path, slots=intent_step.slots)
        sub = plan_from_decision(single, references if index == 0 else ['previous-output'], catalog, tier=tier)
        if index == 0:
            source_ids = sub.sourceAssetIds
        mapping = {step.id: f'{intent_step.id}_{step.id}' for step in sub.steps}
        for step in sub.steps:
            update = {'id': mapping[step.id], 'dependsOn': [mapping[dep] for dep in step.dependsOn],
                'inputFrom': [item.model_copy(update={'stepId': mapping[item.stepId]}) for item in step.inputFrom],
                'parallelGroup': f'{intent_step.id}_{step.parallelGroup}' if step.parallelGroup else None,
                'onFailure': 'fail_run', 'intentStepId': intent_step.id}
            if index and not step.dependsOn:
                update.update(dependsOn=previous, inputFrom=[{'stepId': previous[0], 'index': 0}])
            combined.append(MediaStep.model_validate({**step.model_dump(), **update}))
        previous = [mapping[step.id] for step in sub.steps if not any(step.id in other.dependsOn for other in sub.steps)]
    plan = MultiPlan(goal=decision.trace.get('goal', '；'.join(step.slots['prompt'] for step in steps)),
        goalType='video', tier=tier, steps=combined, sourceAssetIds=source_ids,
        reviewMode=catalog.config.reviewMode,
        maxConcurrency=min(catalog.config.maxConcurrency, *(catalog.resolve(step.capabilityId, tier).binding.maxConcurrency for step in combined)))
    violations = validate_plan(plan, catalog)
    if violations:
        raise HostError(violations[0].code, '顺位计划校验失败', status=422)
    return plan


plan_from_steps = plan_from_multi
