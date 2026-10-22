"""Three deterministic admission checks over the frozen run context; no side effects.

Gate legal verifies each ticket against the locked tool catalog, ownership, scope, budget and
the hard rules (ask_user before undecided video parameters, no video-to-image replanning, no
model switch after blocked_by_model, in-run task fingerprint dedup). Gate types verifies the
reference graph: existing upstreams, declared inputs, kind compatibility and acyclicity.
Gate intent derives the final deliverables (media steps nothing consumes) and compares them
with the run invariants; extra intent rules can be injected for the small-model cross-check
(block 11) through `intent_extensions`.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from math import gcd
from typing import Callable, Iterable, Sequence

from pydantic import ValidationError

from .contracts import (
    AssetRef,
    CanvasAsset,
    DeliverableInvariant,
    Plan,
    PlanStep,
    PlanView,
    ReviewReason,
    ReviewResult,
    RunContext,
    TaskRef,
    ToolSpec,
)

REVIEW_VERSION = "core-review-v1"
MEDIA_OUTPUTS = ("image", "video")
MAX_PARSE_REASONS = 8

# Reserved parameter names used to derive deliverable facts; labels keep reasons model-readable.
PARAM_LABELS = {
    "duration_seconds": "时长",
    "aspect_ratio": "画面比例",
    "count": "数量",
    "prompt": "提示词",
    "width": "宽度",
    "height": "高度",
}
PARAM_UNITS = {"duration_seconds": "秒"}

IntentRule = Callable[[PlanView], list[ReviewReason]]


def intent_reason(code: str, message: str, step: str | None = None) -> ReviewReason:
    """Helper for injected gate-③ rules so their reasons share one shape."""
    return ReviewReason(gate="intent", code=code, step=step, message=message)


def _display(value) -> str:
    """Render 10.0 as 10 so numeric reasons stay model- and user-readable."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _canonical_number(value):
    """JSON number check that maps 8.0 to 8; bool is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def validate(
    plan: Plan | dict,
    run_context: RunContext | dict,
    *,
    intent_extensions: Sequence[IntentRule] | None = None,
) -> ReviewResult:
    """Review a plan against the frozen run context; returns pass or reject with reasons.

    `plan` may be raw JSON (model output or a user's hand edit on the confirmation card) or an
    already parsed Plan; both take the same path. `run_context` must be host-supplied data; a
    malformed context raises instead of rejecting, because it is never the plan's fault.
    """
    context = (
        RunContext.model_validate(run_context) if isinstance(run_context, dict) else run_context
    )
    if isinstance(plan, dict):
        try:
            plan = Plan.model_validate(plan)
        except ValidationError as exc:
            return ReviewResult(verdict="reject", reasons=_parse_reasons(exc))
    reasons: list[ReviewReason] = []
    steps = _index_steps(plan, reasons)
    edges = _build_edges(plan, steps)
    resolved = _gate_legal(plan, steps, edges, context, reasons)
    _gate_types(plan, steps, resolved, context, reasons)
    estimated = _estimate_cost(plan, resolved)
    _gate_intent(
        plan, steps, edges, resolved, context, reasons, intent_extensions=intent_extensions
    )
    return ReviewResult(
        verdict="reject" if reasons else "pass", reasons=reasons, estimated_cost=estimated
    )


def _parse_reasons(exc: ValidationError) -> list[ReviewReason]:
    reasons = []
    for error in exc.errors()[:MAX_PARSE_REASONS]:
        location = ".".join(str(part) for part in error["loc"]) or "plan"
        reasons.append(
            ReviewReason(
                gate="legal",
                code="invalid_plan",
                message=f"计划格式不合法：{location} {error['msg']}",
            )
        )
    return reasons


def _index_steps(plan: Plan, reasons: list[ReviewReason]) -> dict[str, PlanStep]:
    steps: dict[str, PlanStep] = {}
    for step in plan.steps:
        if step.id in steps:
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="duplicate_step_id",
                    step=step.id,
                    message=f"{step.id}: 步骤 id 重复，同一计划内必须唯一",
                )
            )
        steps[step.id] = step
    return steps


def _build_edges(plan: Plan, steps: dict[str, PlanStep]) -> dict[str, list[str]]:
    """Dependency edges combine depends_on and task output references; unknown targets drop."""
    edges: dict[str, list[str]] = {}
    for step in plan.steps:
        targets = list(step.depends_on)
        targets.extend(ref.task for ref in step.inputs.values() if isinstance(ref, TaskRef))
        edges[step.id] = [target for target in targets if target in steps and target != step.id]
    return edges


def _gate_legal(
    plan: Plan,
    steps: dict[str, PlanStep],
    edges: dict[str, list[str]],
    context: RunContext,
    reasons: list[ReviewReason],
) -> dict[str, ToolSpec | None]:
    """Gate ①: each ticket is legal on its own against the frozen catalog, scope and budgets."""
    resolved: dict[str, ToolSpec | None] = {}
    assets = {asset.asset_id: asset for asset in context.canvas.assets}
    owners = {context.user_id, *context.team_user_ids}
    scope_tools = set(context.scope.tools)

    def tool_in_scope(tool: str, version: str) -> bool:
        # Whitelist semantics, same as writable_regions: an empty scope allows nothing.
        return tool in scope_tools or f"{tool}@{version}" in scope_tools

    ask_ancestors = None
    for step in plan.steps:
        for param, ref in step.inputs.items():
            if isinstance(ref, AssetRef):
                _check_asset(step, param, ref, assets, owners, reasons)
        if step.kind == "ask_user":
            if step.tool is not None:
                reasons.append(
                    ReviewReason(
                        gate="legal",
                        code="ask_user_with_tool",
                        step=step.id,
                        message=f"{step.id}: ask_user 步骤不得指定工具，只负责向用户澄清",
                    )
                )
            resolved[step.id] = None
            continue
        if step.tool is None:
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="missing_tool",
                    step=step.id,
                    message=f"{step.id}: call_tool 步骤缺少工具",
                )
            )
            continue
        spec = context.version_lock.tools.get(step.tool)
        if spec is None:
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="tool_not_locked",
                    step=step.id,
                    message=f"{step.id}: 工具 {step.tool} 不在锁定版本目录中",
                )
            )
            continue
        resolved[step.id] = spec
        if not spec.enabled:
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="tool_disabled",
                    step=step.id,
                    message=f"{step.id}: 工具 {spec.id}@{spec.version} 在锁定版本中已停用",
                )
            )
        if step.tool_version is not None and step.tool_version != spec.version:
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="tool_version_mismatch",
                    step=step.id,
                    message=(
                        f"{step.id}: 工具 {spec.id} 版本 {step.tool_version} 与锁定版本 "
                        f"{spec.version} 不一致"
                    ),
                )
            )
        if not tool_in_scope(step.tool, spec.version):
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="tool_out_of_scope",
                    step=step.id,
                    message=f"{step.id}: 工具 {spec.id} 不在本次 Run 的工具 scope 内",
                )
            )
        _check_params(step, spec, reasons)
        if step.region is not None and step.region not in context.scope.writable_regions:
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="region_out_of_scope",
                    step=step.id,
                    message=f"{step.id}: 写入区域 {step.region} 不在本次 Run 的可写区域 scope 内",
                )
            )
        if spec.output_type == "video" and spec.ambiguous_without:
            missing = [name for name in spec.ambiguous_without if name not in step.params]
            if missing:
                if ask_ancestors is None:
                    ask_ancestors = _ancestors(plan, edges)
                if not any(steps[a].kind == "ask_user" for a in ask_ancestors.get(step.id, ())):
                    labels = "、".join(PARAM_LABELS.get(name, name) for name in missing)
                    reasons.append(
                        ReviewReason(
                            gate="legal",
                            code="video_params_uncertain",
                            step=step.id,
                            message=(
                                f"{step.id}: {spec.id} 的{labels}未确定，"
                                "视频参数不确定时必须先安排 ask_user 向用户澄清"
                            ),
                        )
                    )
    _check_replan(plan, resolved, context, reasons)
    _check_fingerprints(plan, resolved, reasons)
    _check_budget(plan, resolved, context, reasons)
    return resolved


def _check_budget(
    plan: Plan,
    resolved: dict[str, ToolSpec | None],
    context: RunContext,
    reasons: list[ReviewReason],
) -> None:
    total = _estimate_cost(plan, resolved)
    if total > context.run_budget:
        reasons.append(
            ReviewReason(
                gate="legal",
                code="run_budget_exceeded",
                message=(f"计划预估花费 {total} 积分，超过本次 Run 预算 {context.run_budget} 积分"),
            )
        )
    if total > context.user_credits:
        reasons.append(
            ReviewReason(
                gate="legal",
                code="credits_exceeded",
                message=f"计划预估花费 {total} 积分，超过用户积分余额 {context.user_credits} 积分",
            )
        )


def _check_params(step: PlanStep, spec: ToolSpec, reasons: list[ReviewReason]) -> None:
    for name, value in step.params.items():
        rule = spec.params_schema.get(name)
        if rule is None:
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="param_unknown",
                    step=step.id,
                    message=f"{step.id}: 工具 {spec.id} 不接受参数 {name}",
                )
            )
            continue
        label = PARAM_LABELS.get(name, name)
        if rule.type == "number" and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="param_type_invalid",
                    step=step.id,
                    message=f"{step.id}: {spec.id} 的{label}需要数字，收到 {value!r}",
                )
            )
            continue
        if rule.type == "string" and not isinstance(value, str):
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="param_type_invalid",
                    step=step.id,
                    message=f"{step.id}: {spec.id} 的{label}需要字符串，收到 {value!r}",
                )
            )
            continue
        if rule.type == "boolean" and not isinstance(value, bool):
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="param_type_invalid",
                    step=step.id,
                    message=f"{step.id}: {spec.id} 的{label}需要布尔值，收到 {value!r}",
                )
            )
            continue
        if rule.enum is not None and value not in rule.enum:
            options = "、".join(str(option) for option in rule.enum)
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="param_enum_invalid",
                    step=step.id,
                    message=f"{step.id}: {spec.id} 的{label}只能是 {options}，收到 {value!r}",
                )
            )
            continue
        unit = PARAM_UNITS.get(name, "")
        spacer = " " if unit else ""
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if rule.minimum is not None and value < rule.minimum:
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="param_below_minimum",
                    step=step.id,
                    message=(
                        f"{step.id}: {spec.id} 的{label} {_display(value)}{spacer}{unit}"
                        f"低于下限 {_display(rule.minimum)}{spacer}{unit}"
                    ),
                )
            )
        if rule.maximum is not None and value > rule.maximum:
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="param_above_maximum",
                    step=step.id,
                    message=(
                        f"{step.id}: {spec.id} 的{label} {_display(value)}{spacer}{unit}"
                        f"超出上限 {_display(rule.maximum)}{spacer}{unit}"
                    ),
                )
            )
    for name, rule in spec.params_schema.items():
        # No exemption for undecided video parameters: plan params are static, an ask_user
        # answer can only arrive through the next plan version, so admitting the ticket now
        # would only move the failure from admission to dispatch time.
        if rule.required and name not in step.params:
            label = PARAM_LABELS.get(name, name)
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="param_required_missing",
                    step=step.id,
                    message=f"{step.id}: {spec.id} 缺少必填参数{label}",
                )
            )


def _check_asset(
    step: PlanStep,
    param: str,
    ref: AssetRef,
    assets: dict[str, CanvasAsset],
    owners: set[str],
    reasons: list[ReviewReason],
) -> None:
    info = assets.get(ref.asset_id)
    if info is None:
        reasons.append(
            ReviewReason(
                gate="legal",
                code="asset_unknown",
                step=step.id,
                message=f"{step.id}: 参数 {param} 引用的资产 {ref.asset_id} 不在画布快照中",
            )
        )
        return
    if info.owner_id not in owners:
        reasons.append(
            ReviewReason(
                gate="legal",
                code="asset_not_owned",
                step=step.id,
                message=(
                    f"{step.id}: 参数 {param} 引用的资产 {ref.asset_id} 属于 {info.owner_id}，"
                    "不在当前用户或其团队名下"
                ),
            )
        )


def _check_replan(
    plan: Plan,
    resolved: dict[str, ToolSpec | None],
    context: RunContext,
    reasons: list[ReviewReason],
) -> None:
    replan = context.replan
    if replan is None:
        return
    previous = {step.id: step for step in replan.previous.steps}
    blocked = set(replan.blocked_task_ids)
    for step in plan.steps:
        before = previous.get(step.id)
        if before is None:
            continue
        before_spec = context.version_lock.tools.get(before.tool) if before.tool else None
        before_out = before_spec.output_type if before_spec else None
        after_out = resolved[step.id].output_type if resolved.get(step.id) else None
        if before_out == "video" and after_out == "image":
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="video_degradation",
                    step=step.id,
                    message=f"{step.id}: 上一版计划中是视频任务，重规划不得改成图片任务",
                )
            )
        if (
            step.id in blocked
            and before.tool is not None
            and step.tool is not None
            and step.tool != before.tool
        ):
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="blocked_model_switch",
                    step=step.id,
                    message=f"{step.id}: 上次执行被模型拦截（blocked_by_model），不得更换模型重做",
                )
            )


def _check_fingerprints(
    plan: Plan, resolved: dict[str, ToolSpec | None], reasons: list[ReviewReason]
) -> None:
    seen: dict[str, str] = {}
    for step in plan.steps:
        if step.kind != "call_tool" or step.tool is None:
            continue
        spec = resolved.get(step.id)
        version = spec.version if spec else (step.tool_version or "")
        inputs = {param: ref.model_dump() for param, ref in sorted(step.inputs.items())}
        payload = json.dumps(
            [step.tool, version, step.params, inputs],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        if fingerprint in seen:
            reasons.append(
                ReviewReason(
                    gate="legal",
                    code="duplicate_task",
                    step=step.id,
                    message=(
                        f"{step.id}: 与 {seen[fingerprint]} 任务指纹重复（相同工具、参数与输入），"
                        "同一 Run 内不得重复派发"
                    ),
                )
            )
        else:
            seen[fingerprint] = step.id


def _gate_types(
    plan: Plan,
    steps: dict[str, PlanStep],
    resolved: dict[str, ToolSpec | None],
    context: RunContext,
    reasons: list[ReviewReason],
) -> None:
    """Gate ②: the reference graph links existing, type-compatible upstreams and stays acyclic."""
    assets = {asset.asset_id: asset for asset in context.canvas.assets}
    for step in plan.steps:
        spec = resolved.get(step.id)
        for dep in step.depends_on:
            if dep not in steps:
                reasons.append(
                    ReviewReason(
                        gate="types",
                        code="missing_dependency",
                        step=step.id,
                        message=f"{step.id}: 依赖的任务 {dep} 不存在",
                    )
                )
        for param, ref in step.inputs.items():
            declared = spec is not None and param in spec.input_types
            if spec is not None and not declared:
                reasons.append(
                    ReviewReason(
                        gate="types",
                        code="undeclared_input",
                        step=step.id,
                        message=f"{step.id}: 输入参数 {param} 不是工具 {spec.id} 声明的输入",
                    )
                )
                continue
            # Reference existence applies to every step kind, ask_user included.
            if isinstance(ref, TaskRef):
                if ref.task not in steps:
                    reasons.append(
                        ReviewReason(
                            gate="types",
                            code="missing_upstream",
                            step=step.id,
                            message=f"{step.id}: 输入参数 {param} 引用了不存在的上游任务 {ref.task}",
                        )
                    )
                    continue
                if declared:
                    upstream_spec = resolved.get(ref.task)
                    upstream_out = upstream_spec.output_type if upstream_spec else None
                    allowed = set(spec.input_types[param])
                    if upstream_out is not None and upstream_out not in allowed:
                        reasons.append(
                            ReviewReason(
                                gate="types",
                                code="input_type_mismatch",
                                step=step.id,
                                message=(
                                    f"{step.id}: 工具 {spec.id} 的 {param} 不接受 "
                                    f"{upstream_out} 类型输入（来自 {ref.task} 的输出）"
                                ),
                            )
                        )
            elif declared:
                info = assets.get(ref.asset_id)
                allowed = set(spec.input_types[param])
                if info is not None and info.kind not in allowed:
                    reasons.append(
                        ReviewReason(
                            gate="types",
                            code="input_type_mismatch",
                            step=step.id,
                            message=(
                                f"{step.id}: 工具 {spec.id} 的 {param} 不接受 "
                                f"{info.kind} 类型资产 {ref.asset_id}"
                            ),
                        )
                    )
    cycle = _find_cycle(plan, steps)
    if cycle:
        chain = " → ".join(cycle + [cycle[0]])
        reasons.append(
            ReviewReason(gate="types", code="dependency_cycle", message=f"计划依赖成环：{chain}")
        )


def _find_cycle(plan: Plan, steps: dict[str, PlanStep]) -> list[str] | None:
    successors: dict[str, list[str]] = {}
    for step in plan.steps:
        targets = set(step.depends_on)
        targets.update(ref.task for ref in step.inputs.values() if isinstance(ref, TaskRef))
        successors[step.id] = [target for target in targets if target in steps]
    color = dict.fromkeys(steps, 0)  # 1 on stack, 2 done
    for root in steps:
        if color[root]:
            continue
        color[root] = 1
        path, iterators = [root], [iter(successors[root])]
        while path:
            nxt = next(iterators[-1], None)
            if nxt is None:
                color[path.pop()] = 2
                iterators.pop()
                continue
            state = color.get(nxt, 2)
            if state == 1:
                return path[path.index(nxt) :] + [nxt]
            if state == 0:
                color[nxt] = 1
                path.append(nxt)
                iterators.append(iter(successors[nxt]))
    return None


def _ancestors(plan: Plan, edges: dict[str, list[str]]) -> dict[str, set[str]]:
    """Transitive ancestors per step over the combined dependency edges."""
    result: dict[str, set[str]] = {}
    for step in plan.steps:
        seen: set[str] = set()
        queue = deque(edges[step.id])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(edges.get(current, ()))
        result[step.id] = seen
    return result


def _estimate_cost(plan: Plan, resolved: dict[str, ToolSpec | None]) -> int:
    total = 0
    for step in plan.steps:
        if step.estimated_cost is not None:
            total += step.estimated_cost
        else:
            spec = resolved.get(step.id)
            total += spec.cost_credits if spec else 0
    return total


def _gate_intent(
    plan: Plan,
    steps: dict[str, PlanStep],
    edges: dict[str, list[str]],
    resolved: dict[str, ToolSpec | None],
    context: RunContext,
    reasons: list[ReviewReason],
    *,
    intent_extensions: Sequence[IntentRule] | None,
) -> None:
    """Gate ③ rule pass: derive final deliverables and compare them with the run invariants."""
    deliverables = _deliverables(plan, edges, resolved)
    assets = {asset.asset_id: asset for asset in context.canvas.assets}
    _check_deliverables(deliverables, steps, edges, resolved, context, assets, reasons)
    if intent_extensions:
        view = PlanView(
            plan=plan,
            context=context,
            tools=resolved,
            deliverable_ids=tuple(step.id for step in deliverables),
        )
        for rule in intent_extensions:
            reasons.extend(rule(view))


def _deliverables(
    plan: Plan, edges: dict[str, list[str]], resolved: dict[str, ToolSpec | None]
) -> list[PlanStep]:
    """Final deliverables are media-producing steps whose output nothing downstream consumes."""
    consumed = {target for targets in edges.values() for target in targets}
    deliverables = []
    for step in plan.steps:
        spec = resolved.get(step.id)
        if spec is not None and spec.output_type in MEDIA_OUTPUTS and step.id not in consumed:
            deliverables.append(step)
    return deliverables


def _check_deliverables(
    deliverables: list[PlanStep],
    steps: dict[str, PlanStep],
    edges: dict[str, list[str]],
    resolved: dict[str, ToolSpec | None],
    context: RunContext,
    assets: dict[str, CanvasAsset],
    reasons: list[ReviewReason],
) -> None:
    invariant = context.invariants.deliverable
    if invariant is None:
        return
    if invariant.modality is not None:
        for step in deliverables:
            output = resolved[step.id].output_type
            if output != invariant.modality:
                reasons.append(
                    ReviewReason(
                        gate="intent",
                        code="modality_mismatch",
                        step=step.id,
                        message=(
                            f"{step.id}: 最终交付物是{output}，与用户要求的{invariant.modality}不符"
                        ),
                    )
                )
    if invariant.count is not None:
        total = sum(_step_count(step) for step in deliverables)
        if total != invariant.count:
            reasons.append(
                ReviewReason(
                    gate="intent",
                    code="deliverable_count",
                    message=f"用户要求 {invariant.count} 个交付物，计划最终只产出 {total} 个",
                )
            )
    if invariant.aspect_ratio is not None or invariant.width is not None:
        _check_deliverable_size(invariant, deliverables, steps, assets, reasons)
    if invariant.duration_seconds is not None:
        for step in deliverables:
            seconds = _resolve_duration(step, steps, assets)
            if seconds is None:
                reasons.append(
                    ReviewReason(
                        gate="intent",
                        code="duration_unknown",
                        step=step.id,
                        message=(
                            f"{step.id}: 无法从计划推算视频时长，无法核对用户要求的 "
                            f"{invariant.duration_seconds} 秒"
                        ),
                    )
                )
            elif seconds != invariant.duration_seconds:
                reasons.append(
                    ReviewReason(
                        gate="intent",
                        code="duration_mismatch",
                        step=step.id,
                        message=(
                            f"{step.id}: 视频时长 {_display(seconds)} 秒，与用户要求的 "
                            f"{invariant.duration_seconds} 秒不符"
                        ),
                    )
                )
    if invariant.required_asset_ids:
        closure = _referenced_assets(deliverables, steps, edges)
        for asset_id in invariant.required_asset_ids:
            if asset_id not in closure:
                reasons.append(
                    ReviewReason(
                        gate="intent",
                        code="required_asset_missing",
                        message=f"必须引用的资产 {asset_id} 未进入最终交付链路",
                    )
                )


def _check_deliverable_size(
    invariant: DeliverableInvariant,
    deliverables: list[PlanStep],
    steps: dict[str, PlanStep],
    assets: dict[str, CanvasAsset],
    reasons: list[ReviewReason],
) -> None:
    exact_size = (
        (invariant.width, invariant.height)
        if invariant.width is not None and invariant.height is not None
        else None
    )
    if invariant.aspect_ratio is None and exact_size is None:
        return
    # Both sides go through the same normalization so 18:32 and 9:16 compare equal.
    expected_ratio = _normalize_ratio_text(invariant.aspect_ratio) or invariant.aspect_ratio
    for step in deliverables:
        if invariant.aspect_ratio is not None:
            ratio = _resolve_ratio(step, steps, assets)
            if ratio is None:
                reasons.append(
                    ReviewReason(
                        gate="intent",
                        code="aspect_unknown",
                        step=step.id,
                        message=(
                            f"{step.id}: 无法从计划推算画面比例，无法核对用户要求的 "
                            f"{invariant.aspect_ratio}"
                        ),
                    )
                )
            elif ratio != expected_ratio:
                reasons.append(
                    ReviewReason(
                        gate="intent",
                        code="aspect_mismatch",
                        step=step.id,
                        message=(
                            f"{step.id}: 产出画面比例 {ratio}，与用户要求的 "
                            f"{invariant.aspect_ratio} 不符"
                        ),
                    )
                )
        elif exact_size is not None:
            size = _resolve_size(step, steps, assets)
            actual = f"{size[0]}×{size[1]}" if size is not None else "无法推算"
            if size != exact_size:
                reasons.append(
                    ReviewReason(
                        gate="intent",
                        code="size_mismatch",
                        step=step.id,
                        message=(
                            f"{step.id}: 产出尺寸 {actual}，与用户要求的 "
                            f"{exact_size[0]}×{exact_size[1]} 不符"
                        ),
                    )
                )


def _step_count(step: PlanStep) -> int:
    value = _canonical_number(step.params.get("count"))
    return int(value) if value is not None and value >= 1 else 1


def _normalize_ratio(width: int, height: int) -> str:
    divisor = gcd(width, height) or 1
    return f"{width // divisor}:{height // divisor}"


def _normalize_ratio_text(value) -> str | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    left, _, right = value.partition(":")
    if left.strip().isdigit() and right.strip().isdigit() and int(left) > 0 and int(right) > 0:
        return _normalize_ratio(int(left), int(right))
    return None


def _resolve_ratio(
    step: PlanStep,
    steps: dict[str, PlanStep],
    assets: dict[str, CanvasAsset],
    memo: dict[str, str | None] | None = None,
) -> str | None:
    memo = memo if memo is not None else {}
    if step.id in memo:
        return memo[step.id]
    memo[step.id] = None
    ratio = _normalize_ratio_text(step.params.get("aspect_ratio"))
    if ratio is None:
        size = _direct_size(step.params)
        if size is not None:
            ratio = _normalize_ratio(int(round(size[0])), int(round(size[1])))
    if ratio is None:
        for ref in step.inputs.values():
            found = None
            if isinstance(ref, TaskRef) and ref.task in steps:
                found = _resolve_ratio(steps[ref.task], steps, assets, memo)
            elif isinstance(ref, AssetRef):
                asset = assets.get(ref.asset_id)
                if asset and asset.width and asset.height:
                    found = _normalize_ratio(asset.width, asset.height)
            if found is not None:
                ratio = found
                break
    memo[step.id] = ratio
    return ratio


def _resolve_size(
    step: PlanStep,
    steps: dict[str, PlanStep],
    assets: dict[str, CanvasAsset],
    memo: dict[str, tuple[int, int] | None] | None = None,
) -> tuple[int, int] | None:
    memo = memo if memo is not None else {}
    if step.id in memo:
        return memo[step.id]
    memo[step.id] = None
    size = _direct_size(step.params)
    if size is None:
        for ref in step.inputs.values():
            found = None
            if isinstance(ref, TaskRef) and ref.task in steps:
                found = _resolve_size(steps[ref.task], steps, assets, memo)
            elif isinstance(ref, AssetRef):
                asset = assets.get(ref.asset_id)
                if asset and asset.width and asset.height:
                    found = (asset.width, asset.height)
            if found is not None:
                size = found
                break
    memo[step.id] = size
    return size


def _direct_size(params: dict) -> tuple[int, int] | None:
    width = _canonical_number(params.get("width"))
    height = _canonical_number(params.get("height"))
    if (
        width is not None
        and height is not None
        and not isinstance(width, float)
        and not isinstance(height, float)
        and width > 0
        and height > 0
    ):
        return width, height
    return None


def _resolve_duration(
    step: PlanStep,
    steps: dict[str, PlanStep],
    assets: dict[str, CanvasAsset],
    memo: dict[str, int | float | None] | None = None,
) -> int | float | None:
    memo = memo if memo is not None else {}
    if step.id in memo:
        return memo[step.id]
    memo[step.id] = None
    seconds = _canonical_number(step.params.get("duration_seconds"))
    if seconds is None or seconds <= 0:
        seconds = None
        for ref in step.inputs.values():
            found = None
            if isinstance(ref, TaskRef) and ref.task in steps:
                found = _resolve_duration(steps[ref.task], steps, assets, memo)
            elif isinstance(ref, AssetRef):
                asset = assets.get(ref.asset_id)
                if asset:
                    found = asset.duration_seconds
            if found is not None:
                seconds = found
                break
    memo[step.id] = seconds
    return seconds


def _referenced_assets(
    deliverables: Iterable[PlanStep], steps: dict[str, PlanStep], edges: dict[str, list[str]]
) -> set[str]:
    """Asset ids referenced anywhere on the transitive input chain of the deliverables."""
    queue = deque(step.id for step in deliverables)
    seen: set[str] = set()
    assets: set[str] = set()
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        step = steps[current]
        assets.update(ref.asset_id for ref in step.inputs.values() if isinstance(ref, AssetRef))
        queue.extend(edges.get(current, ()))
    return assets
