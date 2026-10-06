"""The original single read-only tool loop, kept as the Kernel's default policy.

The Kernel asks the loop two questions: what messages does the next model call
carry, and what does a (valid, still-owned) model answer mean for the Run.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from backend.agent import evidence as ev
from backend.agent import policy as pol
from backend.agent import verifier
from backend.agent.cognition.roles import ANALYST
from backend.agent.context import BuiltContext, Group, build_context
from backend.agent.persona import DEFAULT_PERSONA
from backend.agent.contracts import ControlReceipt, FailureDetails, Finish, NextAction, PersonaProfile, ToolFailure
from backend.agent.orm import AgentModelCall, AgentRun, AgentToolCall
from backend.agent.tools import GATEWAY
from backend.agent.tools.control import ReadEvidenceInput
from backend.agent.util import encoded, error

REPAIR_NOTICE = "上一条回复没有调用任何工具。请调用读取工具获取证据，或调用 finish 工具提交带证据引用的最终回答。"

@dataclass(frozen=True)
class ToolPlan:
    """A tool-call row to record. Read calls may stay pending; control calls resolve inline."""
    request: dict[str, Any]
    kind: str
    status: str
    output: dict | None = None
    error: dict | None = None
    dedupe_of: str | None = None
    tool_version: int | None = None


@dataclass(frozen=True)
class Outcome:
    """tools: record these rows (pending ones execute next); continue: nothing to record;
    end: finish the Run (rows are still recorded for audit)."""
    kind: str
    plans: tuple[ToolPlan, ...] = ()
    status: str | None = None
    error: dict | None = None
    reply: str | None = None
    claims: dict | None = None
    counters: dict | None = None


def toolset_version() -> str:
    return ",".join(f"{name}@{spec.version}" for name, spec in sorted(GATEWAY.specs.items()))


def tool_reserve_bytes() -> int:
    """Room the request needs besides messages: tool schemas and the JSON envelope around them."""
    return len(encoded(GATEWAY.schemas()).encode()) + 1024


def _receipt(request: dict, *, accepted: bool, effect: dict | None = None, code: str | None = None,
             message: str = "", details: FailureDetails | None = None,
             next_actions: list[NextAction] | None = None) -> dict:
    failure = None
    if not accepted:
        spec = ev.FAILURES[code]
        failure = ToolFailure(code=code, category=spec.category, message=message, retryable=spec.retryable,
                              details=details or FailureDetails(), next=next_actions or [])
    return ControlReceipt(tool=request["name"], callId=request["id"], status="accepted" if accepted else "rejected",
                          effect=effect, error=failure).model_dump(mode="json")


def _control_plan(request: dict, receipt: dict) -> ToolPlan:
    failed = receipt["status"] == "rejected"
    return ToolPlan(request, "control", "failed" if failed else "completed", receipt,
                    error(receipt["error"]["code"], receipt["error"]["message"]) if failed else None, None, 1)


class LegacyLoop:
    policy = pol.Policy(GATEWAY)

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
                # Runs created before evidence envelopes only stored a bare output or error.
                content = tool.output if tool.output is not None else {"error": tool.error}
                messages.append({"role": "tool", "tool_call_id": tool.tool_call_id, "content": encoded(content)})
                if tool.kind == "read":
                    evidence_ids.append(ev.evidence_id(tool.ordinal))
            if call.ordinal in repaired:
                messages.append({"role": "user", "content": REPAIR_NOTICE})
            groups.append(Group(messages, tuple(evidence_ids)))
        return groups

    async def context(self, db, run: AgentRun, route) -> BuiltContext:
        persona = PersonaProfile.model_validate(run.persona) if run.persona else DEFAULT_PERSONA
        scene = {"documentId": run.snapshot["id"], "revision": run.snapshot["revision"],
                 "selectedObjectIds": run.snapshot.get("selectedObjectIds", [])}
        return build_context(persona=persona, persona_scope="full", role=ANALYST, scene=scene,
            episodic=list(run.history), memory_sources=list((run.counters or {}).get("memorySources", [])),
            intent=run.message, groups=await self.groups(db, run),
            budget_bytes=route.context_max_bytes - tool_reserve_bytes(), toolset_version=toolset_version())

    @staticmethod
    def _bump(counters: dict, key: str, limit: int) -> tuple[bool, dict]:
        counters[key] = counters.get(key, 0) + 1
        return counters[key] > limit, counters

    def after_model(self, run: AgentRun, call, result, limits, rows: list[dict]) -> Outcome:
        counters = dict(run.counters or {})
        calls = result.tool_calls
        if not calls and result.finish_reason != "stop":
            limited = result.finish_reason == "length"
            return Outcome("end", status="limit_reached" if limited else "failed",
                error=error("MODEL_OUTPUT_LIMIT" if limited else "MODEL_RESPONSE_REJECTED",
                    "模型输出达到长度上限" if limited else "模型未正常完成回答"))
        if not calls:
            exhausted, counters = self._bump(counters, "formatRepairs", pol.MAX_FORMAT_REPAIRS)
            if exhausted:
                return Outcome("end", status="failed", counters=counters,
                    error=error("FORMAT_REPAIR_EXHAUSTED", "模型多次未调用 finish 提交回答"))
            counters["repairAfter"] = [*counters.get("repairAfter", []), call.ordinal]
            return Outcome("continue", counters=counters)
        if len(calls) + run.tool_call_count > limits.max_tool_calls:
            return Outcome("end", status="limit_reached",
                error=error("TOOL_CALL_LIMIT", "整批工具调用超过剩余额度"))
        return self._process(run, calls, rows, counters)

    def _process(self, run: AgentRun, calls: list[dict], rows: list[dict], counters: dict) -> Outcome:
        plans: list[ToolPlan] = []
        work = list(rows)
        batch_ok: dict[str, int] = {}
        ordinal = run.tool_call_count
        claims: dict | None = None
        for request in calls:
            ordinal += 1
            name, arguments = request["name"], request["arguments"]
            meta = dict(tool=name, ordinal=ordinal, call_id=request["id"], step_key=None, snapshot=run.snapshot)
            if name == "finish":
                resolved = self._finish(run, request, work, counters, alone=len(calls) == 1)
                plans.append(resolved["plan"])
                counters, claims = resolved.get("counters", counters), resolved.get("claims", claims)
                if "end" in resolved:
                    return Outcome("end", tuple(plans), claims=claims, counters=counters, **resolved["end"])
                continue
            if name == "read_evidence":
                plans.append(self._read_evidence(request, work))
                continue
            admission = self.policy.admit_read(work, name, arguments, batch_ok)
            if isinstance(admission, pol.Duplicate):
                envelope = ev.duplicate_envelope(**meta, dedupe_of=admission.of,
                    next_actions=[NextAction.model_validate(item) for item in admission.next])
                plan = ToolPlan(request, "read", "completed", envelope, None, admission.of,
                                ev.TOOL_VERSION.get(name, 1))
            elif isinstance(admission, (pol.Reject, pol.Fail)):
                envelope = ev.error_envelope(**meta, code=admission.code, message=admission.message,
                    details=FailureDetails(allowedTools=GATEWAY.read_tools()), next_actions=[])
                plan = ToolPlan(request, "read", "failed", envelope, error(admission.code, admission.message),
                                None, ev.TOOL_VERSION.get(name, 1))
                if isinstance(admission, pol.Fail):
                    plans.append(plan)
                    return Outcome("end", tuple(plans), status="failed", counters=counters,
                                   error=error(admission.code, admission.message))
            else:
                batch_ok[self.policy.key(name, arguments)] = ordinal
                plan = ToolPlan(request, "read", "pending", None, None, None, ev.TOOL_VERSION.get(name, 1))
            plans.append(plan)
            work.append({"ordinal": ordinal, "name": name, "arguments": arguments, "kind": "read",
                         "status": plan.status, "output": plan.output})
        return Outcome("tools", tuple(plans), claims=claims, counters=counters)

    def _read_evidence(self, request: dict, rows: list[dict]) -> ToolPlan:
        try:
            wanted = ReadEvidenceInput.model_validate(request["arguments"]).evidence_id
        except ValidationError as exc:
            return _control_plan(request, _receipt(request, accepted=False, code="INVALID_TOOL_ARGUMENTS",
                message="工具参数无效", details=ev.failure_from_validation(exc)))
        for row in rows:
            output = row.get("output")
            if row.get("kind") == "read" and isinstance(output, dict) and output.get("evidenceId") == wanted:
                return _control_plan(request, _receipt(request, accepted=True, effect={"envelope": output}))
        return _control_plan(request, _receipt(request, accepted=False, code="EVIDENCE_NOT_FOUND",
            message=f"本次运行中不存在证据 {wanted}"))

    def _finish(self, run: AgentRun, request: dict, rows: list[dict], counters: dict, alone: bool) -> dict:
        def reject(code, message, details=None, next_actions=None):
            return _control_plan(request, _receipt(request, accepted=False, code=code, message=message,
                                                   details=details, next_actions=next_actions))
        if not alone:
            exhausted, counters = self._bump(counters, "formatRepairs", pol.MAX_FORMAT_REPAIRS)
            out = {"plan": reject("FORMAT_ERROR", "finish 必须单独调用，不能与其他工具同时调用"), "counters": counters}
            if exhausted:
                out["end"] = {"status": "failed", "error": error("FORMAT_REPAIR_EXHAUSTED", "模型多次未按要求调用 finish")}
            return out
        try:
            finish = Finish.model_validate(request["arguments"])
        except ValidationError as exc:
            exhausted, counters = self._bump(counters, "formatRepairs", pol.MAX_FORMAT_REPAIRS)
            out = {"plan": reject("INVALID_TOOL_ARGUMENTS", "finish 参数无效", ev.failure_from_validation(exc)),
                   "counters": counters}
            if exhausted:
                out["end"] = {"status": "failed", "error": error("FORMAT_REPAIR_EXHAUSTED", "模型多次未按要求调用 finish")}
            return out
        read_rows = [row for row in rows if row.get("kind") == "read"]
        index = verifier.build_index(read_rows, self.policy.key)
        report = verifier.verify_finish(finish, index, run.snapshot["revision"])
        claims = {"answer": finish.answer, "claims": [claim.model_dump(mode="json") for claim in finish.claims],
                  "verification": report.public()}
        if report.passed:
            receipt = _receipt(request, accepted=True, effect={"next": {"stage": "completed"}})
            return {"plan": _control_plan(request, receipt), "claims": claims,
                    "end": {"status": "completed", "reply": finish.answer}}
        if report.fatal:
            return {"plan": reject("CLAIMS_INVALID", "回答引用的证据与运行冻结的画布版本不一致"), "claims": claims,
                    "end": {"status": "failed", "reply": finish.answer,
                            "error": error("EVIDENCE_REVISION_MISMATCH", "回答引用的证据与冻结版本不一致")}}
        exhausted, counters = self._bump(counters, "answerRevisions", pol.MAX_ANSWER_REVISIONS)
        details = FailureDetails(fields=[ev.FieldProblem(path=f"claims.{issue.claim}.evidenceRefs"
            if issue.claim is not None else "answer", problem="value") for issue in report.issues[:20]])
        message = "回答未通过证据检验：" + "；".join(f"{i.check} {i.detail}" for i in report.issues[:6])
        pending = [NextAction.model_validate(item) for item in verifier.required_next_actions(index, finish)][:5]
        plan = reject("CLAIMS_INVALID", message, details, pending)
        if exhausted:
            return {"plan": plan, "claims": claims, "counters": counters,
                    "end": {"status": "limit_reached", "reply": finish.answer,
                            "error": error("ANSWER_UNVERIFIED", "回答多次未通过证据检验")}}
        return {"plan": plan, "claims": claims, "counters": counters}
