"""Text the model reads besides the conversation: working state, reflective memory and the reflection dossier."""
from __future__ import annotations

from backend.agent.state import State

_STATUS = {"pending": "待执行", "running": "进行中", "completed": "已完成", "blocked": "受阻"}
MAX_FACTS_PER_EVIDENCE = 12


def _fmt(value) -> str:
    return " × ".join(str(item) for item in value) if isinstance(value, list) else str(value)


def working_memory(state: State, max_model_calls: int, max_tool_calls: int) -> str:
    lines = ["[工作状态]"]
    plan = state.plan
    if not plan:
        lines.append("目标：尚无计划。")
    else:
        lines.append(f"目标：{plan['goal']}（计划版本 {plan['version']}）")
        for step in state.steps:
            detail = f"- {step.key} [{_STATUS.get(step.status, step.status)}] {step.title}｜完成标准：{step.done_when}"
            if step.summary:
                detail += f"｜结论：{step.summary}"
            if step.evidence_refs:
                detail += f"｜证据：{'、'.join(step.evidence_refs)}"
            lines.append(detail)
        current = state.current
        if state.stage == "execute" and current is not None:
            lines.append(f"当前步骤：{current.key}（允许的工具：{'、'.join(current.tools)}）。{current.intent}")
    pending = _open_issues(state)
    if pending:
        lines.append("需要处理的问题：")
        lines.extend(f"- {issue}" for issue in pending)
    lines.append(f"剩余预算：模型调用 {max(0, max_model_calls - state.model_call_count)} 次，"
                 f"工具调用 {max(0, max_tool_calls - state.tool_call_count)} 次。")
    return "\n".join(lines)


def _open_issues(state: State) -> list[str]:
    """Issues of this Run's latest non-accepting review or hard check, shown until resolved."""
    own = [item for item in state.reflections if item.get("current")]
    if not own or state.stage not in {"execute", "respond", "plan"}:
        return []
    return [f"[{issue['kind']}] {issue['problem']}" for issue in own[-1]["issues"]][:6]


def reflective_memory(state: State) -> str | None:
    """Earlier review findings of this conversation, so a plan does not repeat a rejected approach."""
    earlier = [item for item in state.reflections if not item.get("current")][-6:]
    if not earlier:
        return None
    lines = ["[复盘记忆]（此前的复盘发现的问题，避免重复）"]
    for item in earlier:
        for issue in item["issues"][:2]:
            lines.append(f"- [{item['verdict']}] {issue['problem']}")
    return "\n".join(lines) if len(lines) > 1 else None


def reflection_dossier(state: State) -> str:
    """Everything a reviewer may judge: goal, criteria, claims and facts. Never the author's own reasoning."""
    draft = state.draft or {}
    lines = ["[复盘材料]", f"用户问题：{state.message}"]
    if state.plan:
        lines.append(f"目标：{state.plan['goal']}")
        lines.append("步骤与完成标准：")
        for step in state.steps:
            lines.append(f"- {step.key} [{_STATUS.get(step.status, step.status)}] {step.title}｜完成标准：{step.done_when}"
                         f"｜结论：{step.summary or '（无）'}｜证据：{'、'.join(step.evidence_refs) or '（无）'}")
    lines.append(f"草稿回答：{draft.get('answer', '')}")
    lines.append("断言：")
    for number, claim in enumerate(draft.get("claims", []), 1):
        lines.append(f"{number}. ({claim['scope']}) {claim['text']} ← {'、'.join(claim['evidenceRefs']) or '无引用'}")
    lines.append("证据：")
    for row in state.read_rows:
        output = row.get("output")
        if not isinstance(output, dict) or output.get("kind") != "tool":
            continue
        ident, status = output["evidenceId"], output["status"]
        step = next((item.key for item in state.steps if item.id == row.get("step_id")), "-")
        if status == "error":
            lines.append(f"{ident}（{output['tool']}，步骤 {step}）失败：{output['error']['code']}")
        elif status == "duplicate":
            lines.append(f"{ident}（{output['tool']}，步骤 {step}）与 {output['dedupeOf']} 重复")
        else:
            result = output["result"]
            coverage = "完整" if result["coverage"]["complete"] else f"部分：{result['coverage'].get('reason') or '未覆盖全部'}"
            lines.append(f"{ident}（{output['tool']}，步骤 {step}，画布版本 {output['source']['revision']}）{coverage}")
            for fact in result["evidence"][:MAX_FACTS_PER_EVIDENCE]:
                lines.append(f"  {fact['id']} {fact['statement']}：{_fmt(fact['value'])}（{fact['scope']}）")
    verification = draft.get("verification")
    if verification:
        lines.append("程序检验：" + ("H1–H6 全部通过" if verification["passed"] else "存在未通过项"))
    lines.append("请对照以上材料调用 submit_reflection。")
    return "\n".join(lines)
