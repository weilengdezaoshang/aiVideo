"""The loop policy of the Kernel: loads a Run's State, builds the stage's request, decides outcomes.

I/O lives here (queries); every decision is delegated to the pure orchestrator.
"""
from __future__ import annotations

from sqlalchemy import select

from backend.agent import orchestrator
from backend.agent.cognition import briefing
from backend.agent.cognition.roles import PERSONA_SCOPE_BY_STAGE, ROLE_BY_STAGE, stage_tools
from backend.agent.context import BuiltContext, Group, build_context
from backend.agent.contracts import PersonaProfile
from backend.agent.orchestrator import POLICY, Outcome
from backend.agent.orm import AgentModelCall, AgentReflection, AgentRun, AgentStep, AgentToolCall
from backend.agent.persona import DEFAULT_PERSONA
from backend.agent.state import State, StepState
from backend.agent.tools import GATEWAY
from backend.agent.util import encoded

REPAIR_NOTICE = "上一条回复没有调用任何工具。请使用当前可用的工具继续；最终回答、计划、步骤结论和复盘都必须通过对应的工具提交。"


def toolset_version() -> str:
    return ",".join(f"{name}@{spec.version}" for name, spec in sorted(GATEWAY.specs.items()))


def _step_state(row: AgentStep) -> StepState:
    return StepState(str(row.id), row.key, row.ordinal, row.title, row.intent, tuple(row.allowed_tools),
                     tuple(row.depends_on), row.done_when, row.status, row.summary,
                     tuple(row.evidence_refs or ()), row.retries)


class HarnessLoop:
    policy = POLICY

    async def load(self, db, run: AgentRun) -> State:
        version = (run.plan or {}).get("version")
        step_rows = list(await db.scalars(select(AgentStep).where(AgentStep.run_id == run.id)
                                          .order_by(AgentStep.plan_version, AgentStep.ordinal)))
        steps = tuple(_step_state(row) for row in step_rows if row.plan_version == version)
        rows = [{"ordinal": c.ordinal, "name": c.name, "arguments": c.arguments, "status": c.status,
                 "kind": c.kind, "output": c.output, "dedupe_of": c.dedupe_of,
                 "step_id": str(c.step_id) if c.step_id else None}
                for c in await db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id)
                                          .order_by(AgentToolCall.ordinal))]
        own = [{"verdict": r.verdict, "issues": r.issues, "source": r.source, "current": True}
               for r in await db.scalars(select(AgentReflection).where(AgentReflection.run_id == run.id)
                                         .order_by(AgentReflection.created_at))]
        earlier = [{"verdict": r.verdict, "issues": r.issues, "source": r.source, "current": False}
                   for r in reversed(list(await db.scalars(select(AgentReflection).where(
                       AgentReflection.session_id == run.session_id, AgentReflection.run_id != run.id,
                       AgentReflection.verdict != "accept").order_by(AgentReflection.created_at.desc()).limit(20))))]
        model_calls = await db.scalar(select(AgentModelCall.ordinal).where(AgentModelCall.run_id == run.id)
                                      .order_by(AgentModelCall.ordinal.desc()).limit(1)) or 0
        return State(stage=run.stage, snapshot=run.snapshot, message=run.message, counters=dict(run.counters or {}),
                     rows=tuple(rows), tool_call_count=run.tool_call_count, model_call_count=model_calls,
                     plan=run.plan, steps=steps, draft=run.claims, waiting=run.waiting,
                     reflections=tuple([*earlier, *own]))

    async def groups(self, db, run: AgentRun) -> list[Group]:
        """Completed model turns as the next request replays them, oldest first."""
        repaired = set((run.counters or {}).get("repairAfter", []))
        groups: list[Group] = []
        for call in await db.scalars(select(AgentModelCall).where(AgentModelCall.run_id == run.id,
                AgentModelCall.status == "completed").order_by(AgentModelCall.ordinal)):
            messages, evidence_ids = [call.response], []
            for tool in await db.scalars(select(AgentToolCall).where(AgentToolCall.model_call_id == call.id)
                    .order_by(AgentToolCall.ordinal)):
                if tool.status not in {"completed", "failed"}:
                    raise RuntimeError("Incomplete tool results cannot enter model context")
                content = tool.output if tool.output is not None else {"error": tool.error}
                messages.append({"role": "tool", "tool_call_id": tool.tool_call_id, "content": encoded(content)})
                if tool.kind == "read" and isinstance(tool.output, dict) and "evidenceId" in tool.output:
                    evidence_ids.append(tool.output["evidenceId"])
            if call.ordinal in repaired:
                messages.append({"role": "user", "content": REPAIR_NOTICE})
            groups.append(Group(messages, tuple(evidence_ids)))
        return groups

    async def context(self, db, run: AgentRun, route, limits) -> BuiltContext:
        state = await self.load(db, run)
        stage = state.stage
        persona = PersonaProfile.model_validate(run.persona) if run.persona else DEFAULT_PERSONA
        current = state.current if stage == "execute" else None
        names = stage_tools(stage, current.tools if current else ())
        tools = GATEWAY.schemas(names)
        working, reflective = [], []
        if stage == "reflect":
            working.append({"role": "user", "content": briefing.reflection_dossier(state)})
        else:
            working.append({"role": "user", "content": briefing.working_memory(
                state, limits.max_model_calls, limits.max_tool_calls)})
            lesson = briefing.reflective_memory(state) if stage in {"plan", "respond"} else None
            if lesson:
                reflective.append({"role": "user", "content": lesson})
        scene = {"documentId": run.snapshot["id"], "revision": run.snapshot["revision"],
                 "selectedObjectIds": run.snapshot.get("selectedObjectIds", [])}
        reserve = len(encoded(tools).encode()) + 1024
        return build_context(persona=persona, persona_scope=PERSONA_SCOPE_BY_STAGE[stage], role=ROLE_BY_STAGE[stage],
            scene=scene, episodic=list(run.history), memory_sources=list((run.counters or {}).get("memorySources", [])),
            intent=run.message, groups=[] if stage == "reflect" else await self.groups(db, run),
            budget_bytes=route.context_max_bytes - reserve, toolset_version=toolset_version(), working=working,
            reflective=reflective, tools=tools, stage=stage, step_id=current.id if current else None)

    async def decide(self, db, run: AgentRun, call, result, limits) -> Outcome:
        state = await self.load(db, run)
        return orchestrator.on_model(state, list(result.tool_calls), result.finish_reason, result.content,
                                     call.ordinal, limits)
