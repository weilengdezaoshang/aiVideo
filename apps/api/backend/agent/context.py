"""Layered context assembly under a byte budget, with a manifest of what was sent.

Required layers (rules, persona, role, scene, intent) are never trimmed; when they do
not fit the Run stops instead of silently dropping a protection. Optional layers go in
this order: episodic memory, then older observations (compacted, newest kept whole).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from backend.agent import persona as personas
from backend.agent.contracts import PersonaProfile
from backend.agent.cognition.roles import RULES, RULES_VERSION, Role
from backend.agent.util import encoded


class ContextLimit(Exception):
    """The required layers alone exceed the budget."""


@dataclass(frozen=True)
class Group:
    """One model turn as the next request replays it: assistant message, its tool replies, notices."""
    messages: list[dict]
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuiltContext:
    messages: list[dict]
    manifest: dict = field(default_factory=dict)
    tools: list[dict] = field(default_factory=list)
    stage: str | None = None
    step_id: str | None = None


def size_of(messages: list[dict]) -> int:
    return len(encoded(messages).encode())


def compact_content(content: str) -> str:
    """Drop bulky data from an old tool message but keep facts, coverage and next steps."""
    try:
        value = json.loads(content)
    except ValueError:
        return content
    if not isinstance(value, dict):
        return content
    if value.get("kind") == "tool" and value.get("status") == "ok":
        value["result"] = {**value["result"], "data": {"compacted": True}}
    elif value.get("kind") == "control" and isinstance(value.get("effect"), dict) \
            and "envelope" in value["effect"]:
        value["effect"] = {"compacted": True}
    else:
        return content
    return encoded(value)


def _compact_group(group: Group) -> Group:
    messages = [{**message, "content": compact_content(message["content"])} if message["role"] == "tool"
                else message for message in group.messages]
    return Group(messages, group.evidence_ids)


def build_context(*, persona: PersonaProfile, persona_scope: str, role: Role, scene: dict,
                  episodic: list[dict], memory_sources: list[str], intent: str, groups: list[Group],
                  budget_bytes: int, toolset_version: str, working: list[dict] = (),
                  reflective: list[dict] = (), tools: list[dict] = (), stage: str | None = None,
                  step_id: str | None = None, attachments: list[dict] = ()) -> BuiltContext:
    """``attachments`` is the reserved reflection attachment layer (validation reports of the
    generation loop). Empty this round; like ``working`` it is never trimmed — dropping an
    approval or validation protection silently is never acceptable."""
    system = {"role": "system", "content": "\n\n".join([
        f"[规则 v{RULES_VERSION}]\n{RULES}", personas.render(persona, persona_scope),
        f"[角色 {role.id}@v{role.version}]\n{role.text}", "当前上下文：" + encoded(scene)])}
    user = {"role": "user", "content": intent}
    trimmed: list[dict] = []
    memory, observed, lessons = list(episodic), list(groups), list(reflective)

    def assemble() -> list[dict]:
        # Working state comes last so the newest instruction is the current state of the Run.
        return [system, *memory, user, *(message for group in observed for message in group.messages),
                *lessons, *working, *attachments]

    if size_of([system, user, *working, *attachments]) > budget_bytes:
        raise ContextLimit()
    while size_of(assemble()) > budget_bytes and memory:
        memory = memory[2:]
        trimmed.append({"layer": "episodic", "action": "dropped_oldest_pair"})
    if size_of(assemble()) > budget_bytes and lessons:
        lessons = []
        trimmed.append({"layer": "reflection", "action": "dropped"})
    for position in range(len(observed) - 1):
        if size_of(assemble()) <= budget_bytes:
            break
        observed[position] = _compact_group(observed[position])
        trimmed.append({"layer": "observation", "action": "compacted", "evidenceIds": list(observed[position].evidence_ids)})
    messages = assemble()
    if size_of(messages) > budget_bytes:
        raise ContextLimit()
    kept = len(memory) // 2 if episodic else 0
    manifest = {
        "rulesVersion": RULES_VERSION, "personaId": persona.id, "personaVersion": persona.version,
        "roleId": role.id, "roleVersion": role.version, "toolsetVersion": toolset_version,
        "stage": stage, "stepId": step_id, "tools": [item["function"]["name"] for item in tools],
        "layers": [
            {"name": "system", "bytes": size_of([system]), "trimmed": False},
            {"name": "intent", "bytes": size_of([user]), "trimmed": False},
            {"name": "working", "bytes": size_of(list(working)) if working else 0, "trimmed": False},
            {"name": "attachment", "bytes": size_of(list(attachments)) if attachments else 0, "trimmed": False},
            {"name": "reflection", "bytes": size_of(lessons) if lessons else 0, "trimmed": len(lessons) < len(reflective)},
            {"name": "episodic", "bytes": size_of(memory) if memory else 0, "trimmed": len(memory) < len(episodic)},
            {"name": "observation", "bytes": size_of([m for g in observed for m in g.messages]) if observed else 0,
             "trimmed": any(item["layer"] == "observation" for item in trimmed)}],
        "trimmed": trimmed, "memorySources": memory_sources[len(memory_sources) - kept:] if kept else [],
        "evidenceIds": [ident for group in observed for ident in group.evidence_ids],
        "budgetBytes": budget_bytes, "totalBytes": size_of(messages)}
    return BuiltContext(messages, manifest, list(tools), stage, step_id)
