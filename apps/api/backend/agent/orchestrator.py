"""Stage state machine of the harness as a pure function: (state, model answer) -> outcome.

plan -> execute(step...) -> respond -> reflect -> done. Nothing here touches I/O: the loop loads
a ``State`` and the kernel applies the returned ``Outcome`` in one transaction. The model only
*proposes*; every transition is decided here from stored evidence and the policy budgets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from backend.agent import evidence as ev
from backend.agent import policy as pol
from backend.agent import verifier
from backend.agent.cognition.planner import execution_order, validate_plan
from backend.agent.cognition.roles import stage_tools
from backend.agent.contracts import (
    ControlReceipt, FailureDetails, Finish, NextAction, Plan, Reflection, ToolFailure,
)
from backend.agent.state import State, StepState
from backend.agent.tools import GATEWAY
from backend.agent.tools.control import AskUserInput, CompleteStepInput, ReadEvidenceInput, ReportBlockerInput
from backend.agent.util import error

MAX_STEP_TOOL_CALLS = 6
MAX_PLAN_REVISIONS = 2
MAX_STEP_RETRIES = 1
MAX_REFLECT_ROUNDS = 3
READ_TOOLS = set(GATEWAY.read_tools())
POLICY = pol.Policy(GATEWAY)
_ISSUE_KIND = {"H1": "ungrounded", "H2": "ungrounded", "H3": "truncated", "H4": "ungrounded", "H6": "incomplete"}


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
    step_id: str | None = None


@dataclass
class Outcome:
    """What to record after a model answer. ``end`` finishes the Run (rows are still recorded)."""
    kind: str = "continue"                       # tools | continue | end
    plans: list[ToolPlan] = field(default_factory=list)
    run: dict = field(default_factory=dict)      # attribute updates for the Run
    steps: list[dict] = field(default_factory=list)
    reflections: list[dict] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    status: str | None = None
    error: dict | None = None
    reply: str | None = None


def _receipt(request: dict, *, accepted: bool, effect: dict | None = None, code: str | None = None,
             message: str = "", details: FailureDetails | None = None,
             next_actions: list[NextAction] | None = None) -> dict:
    failure = None
    if not accepted:
        spec = ev.FAILURES[code]
        failure = ToolFailure(code=code, category=spec.category, message=message, retryable=spec.retryable,
                              details=details or FailureDetails(), next=next_actions or [])
    return ControlReceipt(tool=request["name"], callId=request["id"],
                          status="accepted" if accepted else "rejected", effect=effect,
                          error=failure).model_dump(mode="json")


def control_plan(request: dict, receipt: dict, step_id: str | None = None) -> ToolPlan:
    failed = receipt["status"] == "rejected"
    return ToolPlan(request, "control", "failed" if failed else "completed", receipt,
                    error(receipt["error"]["code"], receipt["error"]["message"]) if failed else None,
                    None, 1, step_id)


def _issue(item: verifier.Issue) -> dict:
    return {"kind": _ISSUE_KIND.get(item.check, "ungrounded"), "problem": item.detail, "stepKey": None,
            "evidenceRefs": [item.ref] if item.ref else []}


def on_model(state: State, calls: list, finish_reason: str, content: str | None, model_ordinal: int,
             limits) -> Outcome:
    turn = _Turn(state, limits)
    if not calls and finish_reason != "stop":
        limited = finish_reason == "length"
        return turn.finish("limit_reached" if limited else "failed",
            error("MODEL_OUTPUT_LIMIT" if limited else "MODEL_RESPONSE_REJECTED",
                  "模型输出达到长度上限" if limited else "模型未正常完成回答"))
    if not calls:
        if turn.bump("formatRepairs", pol.MAX_FORMAT_REPAIRS):
            return turn.finish("failed", error("FORMAT_REPAIR_EXHAUSTED", "模型多次未调用工具"))
        turn.counters["repairAfter"] = [*turn.counters.get("repairAfter", []), model_ordinal]
        return turn.outcome()
    if len(calls) + state.tool_call_count > limits.max_tool_calls:
        return turn.finish("limit_reached", error("TOOL_CALL_LIMIT", "整批工具调用超过剩余额度"))
    for request in calls:
        turn.handle(request, alone=len(calls) == 1)
        if turn.ended or turn.moved:
            break
    return turn.outcome()


class _Turn:
    def __init__(self, state: State, limits):
        self.s, self.limits = state, limits
        self.stage = state.stage
        self.counters = dict(state.counters)
        self.out = Outcome()
        self.work = list(state.rows)
        self.evidence_no = len(state.read_rows)
        self.batch_ok: dict[str, int] = {}
        self.ended = False
        self.moved = False
        self.step_of = {row["output"]["evidenceId"]: row.get("step_id") for row in state.read_rows
                        if isinstance(row.get("output"), dict) and row["output"].get("kind") == "tool"}

    # -- bookkeeping ---------------------------------------------------------------------
    def bump(self, key: str, limit: int) -> bool:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key] > limit

    def outcome(self) -> Outcome:
        out = self.out
        out.run["counters"] = self.counters
        if out.kind != "end":
            out.kind = "tools" if out.plans else "continue"
        return out

    def finish(self, status: str, failure: dict | None, reply: str | None = None) -> Outcome:
        self.out.kind, self.out.status, self.out.error, self.out.reply = "end", status, failure, reply
        if status == "completed":
            self.out.run["stage"] = "done"
        self.ended = True
        return self.outcome()

    def goto(self, stage: str) -> None:
        self.stage = stage
        self.moved = True
        self.out.run["stage"] = stage
        self.counters["formatRepairs"] = 0
        self.out.events.append("stage.entered")

    def add(self, plan: ToolPlan) -> None:
        self.out.plans.append(plan)
        self.work.append({"name": plan.request["name"], "arguments": plan.request["arguments"], "kind": plan.kind,
                          "status": plan.status, "output": plan.output, "step_id": plan.step_id})

    def reject(self, request: dict, code: str, message: str, details: FailureDetails | None = None,
               next_actions: list[NextAction] | None = None, step_id: str | None = None) -> None:
        self.add(control_plan(request, _receipt(request, accepted=False, code=code, message=message,
                                                details=details, next_actions=next_actions), step_id))

    def format_error(self, request: dict, code: str, message: str, details: FailureDetails | None = None) -> None:
        self.reject(request, code, message, details)
        if self.bump("formatRepairs", pol.MAX_FORMAT_REPAIRS):
            self.finish("failed", error("FORMAT_REPAIR_EXHAUSTED", "模型多次未按要求调用控制工具"))

    def meta(self, request: dict, number: int) -> dict:
        return dict(tool=request["name"], ordinal=number, call_id=request["id"], step_key=None,
                    snapshot=self.s.snapshot)

    def allowed(self) -> list[str]:
        current = self.s.current
        return stage_tools(self.stage, current.tools if current else ())

    # -- dispatch -------------------------------------------------------------------------
    def handle(self, request: dict, alone: bool) -> None:
        name = request["name"]
        spec = GATEWAY.specs.get(name)
        if spec is None or name not in self.allowed():
            code, message = (("UNKNOWN_TOOL", "工具不存在") if spec is None
                             else ("TOOL_NOT_ALLOWED", "该工具在当前阶段或步骤不可用"))
            if spec is None or spec.kind == "read":
                self.read_refused(request, code, message)
            else:
                self.reject(request, "TOOL_NOT_ALLOWED", message, FailureDetails(allowedTools=self.allowed()))
            return
        if spec.kind == "read":
            return self.read(request)
        if name == "read_evidence":
            return self.read_evidence(request)
        if not alone:
            return self.format_error(request, "FORMAT_ERROR", f"{name} 必须单独调用，不能与其他工具同时调用")
        getattr(self, "do_" + name)(request)

    def read_refused(self, request: dict, code: str, message: str) -> None:
        self.evidence_no += 1
        envelope = ev.error_envelope(**self.meta(request, self.evidence_no), code=code, message=message,
                                     details=FailureDetails(allowedTools=self.allowed()), next_actions=[])
        current = self.s.current
        self.add(ToolPlan(request, "read", "failed", envelope, error(code, message), None,
                          ev.TOOL_VERSION.get(request["name"], 1), current.id if current else None))

    # -- read tools -----------------------------------------------------------------------
    def read(self, request: dict) -> None:
        name, arguments = request["name"], request["arguments"]
        current = self.s.current if self.stage == "execute" else None
        step_id = current.id if current else None
        used = sum(1 for row in self.work if row.get("kind") == "read" and row.get("step_id") == step_id)
        if current and used >= MAX_STEP_TOOL_CALLS:
            return self.read_refused(request, "TOOL_NOT_ALLOWED",
                f"步骤 {current.key} 的工具调用已达 {MAX_STEP_TOOL_CALLS} 次上限，请 complete_step 或 report_blocker")
        admission = POLICY.admit_read(self.work, name, arguments, self.batch_ok)
        self.evidence_no += 1
        number = self.evidence_no
        meta, version = self.meta(request, number), ev.TOOL_VERSION.get(name, 1)
        if isinstance(admission, pol.Duplicate):
            envelope = ev.duplicate_envelope(**meta, dedupe_of=admission.of,
                next_actions=[NextAction.model_validate(item) for item in admission.next])
            return self.add(ToolPlan(request, "read", "completed", envelope, None, admission.of, version, step_id))
        if isinstance(admission, (pol.Reject, pol.Fail)):
            envelope = ev.error_envelope(**meta, code=admission.code, message=admission.message,
                details=FailureDetails(allowedTools=sorted(READ_TOOLS)), next_actions=[])
            self.add(ToolPlan(request, "read", "failed", envelope, error(admission.code, admission.message),
                              None, version, step_id))
            if isinstance(admission, pol.Fail):
                self.finish("failed", error(admission.code, admission.message))
            return
        self.batch_ok[POLICY.key(name, arguments)] = number
        self.add(ToolPlan(request, "read", "pending", None, None, None, version, step_id))

    def read_evidence(self, request: dict) -> None:
        try:
            wanted = ReadEvidenceInput.model_validate(request["arguments"]).evidence_id
        except ValidationError as exc:
            return self.reject(request, "INVALID_TOOL_ARGUMENTS", "工具参数无效", ev.failure_from_validation(exc))
        for row in self.work:
            output = row.get("output")
            if row.get("kind") == "read" and isinstance(output, dict) and output.get("evidenceId") == wanted:
                return self.add(control_plan(request, _receipt(request, accepted=True, effect={"envelope": output})))
        self.reject(request, "EVIDENCE_NOT_FOUND", f"本次运行中不存在证据 {wanted}")

    def index(self) -> dict[str, verifier.EvidenceEntry]:
        return verifier.build_index([row for row in self.work if row.get("kind") == "read"], POLICY.key)

    # -- plan stage -------------------------------------------------------------------------
    def do_submit_plan(self, request: dict) -> None:
        try:
            plan = Plan.model_validate(request["arguments"])
        except ValidationError as exc:
            return self.format_error(request, "INVALID_TOOL_ARGUMENTS", "submit_plan 参数无效", ev.failure_from_validation(exc))
        issues = validate_plan(plan, READ_TOOLS)
        if issues:
            details = FailureDetails(fields=[ev.FieldProblem(path=i.path, problem=i.problem) for i in issues[:20]])
            return self.format_error(request, "PLAN_INVALID",
                "计划不合法：步骤 key 唯一、每步至少一个已注册的只读工具、依赖存在且无环", details)
        ordered = execution_order(plan.steps)
        version = (self.s.plan or {}).get("version", 0) + 1
        if self.s.steps:
            self.out.steps.append({"op": "obsolete"})
        self.out.steps.append({"op": "create", "version": version, "steps": [
            {"key": step.key, "ordinal": index, "title": step.title, "intent": step.intent, "tools": step.tools,
             "depends_on": step.dependsOn, "done_when": step.doneWhen,
             "status": "running" if index == 1 else "pending"} for index, step in enumerate(ordered, 1)]})
        first = ordered[0].key
        self.out.run["plan"] = {"goal": plan.goal, "version": version, "current": first}
        self.out.events.append("plan.submitted")
        self.add(control_plan(request, _receipt(request, accepted=True, effect={
            "planVersion": version, "steps": [{"key": s.key} for s in ordered],
            "next": {"stage": "execute", "stepKey": first}})))
        self.goto("execute")

    def do_ask_user(self, request: dict) -> None:
        try:
            question = AskUserInput.model_validate(request["arguments"]).question
        except ValidationError as exc:
            return self.format_error(request, "INVALID_TOOL_ARGUMENTS", "ask_user 参数无效", ev.failure_from_validation(exc))
        self.out.run["waiting"] = {"question": question, "stage": self.stage}
        self.out.run["stage"] = "waiting"
        self.add(control_plan(request, _receipt(request, accepted=True, effect={"waiting": "input"})))
        self.finish("waiting_input", None)

    # -- execute stage ----------------------------------------------------------------------
    def do_complete_step(self, request: dict) -> None:
        step = self.s.current
        if step is None:
            return self.reject(request, "TOOL_NOT_ALLOWED", "当前没有进行中的步骤", FailureDetails(allowedTools=self.allowed()))
        try:
            args = CompleteStepInput.model_validate(request["arguments"])
        except ValidationError as exc:
            return self.format_error(request, "INVALID_TOOL_ARGUMENTS", "complete_step 参数无效", ev.failure_from_validation(exc))
        index, problems = self.index(), []
        if not args.evidence_refs:
            problems.append("没有引用本步骤取得的证据")
        for ref in dict.fromkeys(args.evidence_refs):
            found = verifier.resolve_ref(index, ref)
            envelope_id = ref.split(".")[0]
            if found is None:
                problems.append(f"{ref} 不存在于本次运行的证据中")
            elif self.step_of.get(envelope_id) != step.id:
                problems.append(f"{ref} 不是本步骤取得的证据")
            elif found[0].status != "ok":
                problems.append(f"{ref} 是失败的工具结果")
        pending = []
        for row in self.s.read_rows:
            output = row.get("output")
            if row.get("step_id") == step.id and isinstance(output, dict) and output.get("kind") == "tool" \
                    and output.get("status") == "ok":
                pending.extend(index[output["evidenceId"]].open_required)
        pending = list({repr(item): item for item in pending}.values())
        if pending and not args.partial:
            problems.append("本步骤仍有必需的后续读取；请先执行，或设置 partial=true 说明结论只是部分的")
        if problems:
            return self.reject(request, "STEP_UNSUPPORTED", "步骤尚不能完成：" + "；".join(problems),
                next_actions=[NextAction.model_validate(item) for item in pending][:5], step_id=step.id)
        summary = ("（部分）" if args.partial else "") + args.summary
        self.out.steps.append({"op": "update", "key": step.key, "fields": {
            "status": "completed", "summary": summary, "evidence_refs": list(dict.fromkeys(args.evidence_refs))}})
        following = next((item for item in self.s.steps if item.status == "pending"), None)
        self.out.events.append("step.completed")
        if following is not None:
            self.out.steps.append({"op": "update", "key": following.key, "fields": {"status": "running"}})
            self.out.run["plan"] = {**self.s.plan, "current": following.key}
            effect = {"next": {"stage": "execute", "stepKey": following.key}}
        else:
            effect = {"next": {"stage": "respond"}}
        self.add(control_plan(request, _receipt(request, accepted=True, effect=effect), step.id))
        if following is None:
            self.goto("respond")

    def do_report_blocker(self, request: dict) -> None:
        step = self.s.current
        if step is None:
            return self.reject(request, "TOOL_NOT_ALLOWED", "当前没有进行中的步骤", FailureDetails(allowedTools=self.allowed()))
        try:
            args = ReportBlockerInput.model_validate(request["arguments"])
        except ValidationError as exc:
            return self.format_error(request, "INVALID_TOOL_ARGUMENTS", "report_blocker 参数无效", ev.failure_from_validation(exc))
        self.out.steps.append({"op": "update", "key": step.key, "fields": {"status": "blocked", "summary": args.detail}})
        self.out.reflections.append({"source": "blocker", "verdict": "replan", "hard_checks": None, "question": None,
            "issues": [{"kind": "incomplete", "problem": f"{args.code}：{args.detail}", "stepKey": step.key,
                        "evidenceRefs": []}]})
        self.out.events.extend(["step.blocked", "reflection.recorded"])
        exhausted = self.bump("planRevisions", MAX_PLAN_REVISIONS)
        self.add(control_plan(request, _receipt(request, accepted=True, effect={
            "next": {"stage": "plan"}, "planRevisionsLeft": max(0, MAX_PLAN_REVISIONS - self.counters["planRevisions"])}), step.id))
        if exhausted:
            return self.finish("limit_reached", error("PLAN_REVISIONS_EXHAUSTED", "多次重新规划后仍无法完成"))
        self.goto("plan")

    # -- respond stage ----------------------------------------------------------------------
    def do_finish(self, request: dict) -> None:
        try:
            finish = Finish.model_validate(request["arguments"])
        except ValidationError as exc:
            return self.format_error(request, "INVALID_TOOL_ARGUMENTS", "finish 参数无效", ev.failure_from_validation(exc))
        index = self.index()
        report = verifier.verify_finish(finish, index, self.s.revision)
        payload = {"answer": finish.answer, "claims": [claim.model_dump(mode="json") for claim in finish.claims],
                   "verification": report.public()}
        self.out.run["claims"] = payload
        if report.passed:
            self.add(control_plan(request, _receipt(request, accepted=True, effect={"next": {"stage": "reflect"}})))
            return self.goto("reflect")
        issues = [_issue(item) for item in report.issues[:8]]
        if report.fatal:
            self.add(control_plan(request, _receipt(request, accepted=False, code="CLAIMS_INVALID",
                message="回答引用的证据与运行冻结的画布版本不一致")))
            return self.finish("failed", error("EVIDENCE_REVISION_MISMATCH", "回答引用的证据与冻结版本不一致"), finish.answer)
        message = "回答未通过证据检验：" + "；".join(f"{i.check} {i.detail}" for i in report.issues[:6])
        details = FailureDetails(fields=[ev.FieldProblem(
            path=f"claims.{i.claim}.evidenceRefs" if i.claim is not None else "answer", problem="value")
            for i in report.issues[:20]])
        pending = [NextAction.model_validate(item) for item in verifier.required_next_actions(index, finish)][:5]
        retry = self.retry_target(report)
        verdict = "retry_step" if retry else "revise_answer"
        self.out.reflections.append({"source": "hard_check", "verdict": verdict, "hard_checks": report.public(),
                                     "question": None, "issues": issues})
        self.out.events.append("reflection.recorded")
        self.reject(request, "CLAIMS_INVALID", message + ("；将回到对应步骤重试" if retry else ""), details, pending)
        if retry:
            return self.back_to_step(retry)
        if self.bump("answerRevisions", pol.MAX_ANSWER_REVISIONS):
            self.finish("limit_reached", error("ANSWER_UNVERIFIED", "回答多次未通过证据检验"), finish.answer)

    def retry_target(self, report: verifier.Report) -> StepState | None:
        """Error evidence that was cited as support sends the Run back to the step that produced it."""
        for item in report.issues:
            if item.check == "H2" and item.ref:
                step_id = self.step_of.get(item.ref.split(".")[0])
                step = next((s for s in self.s.steps if s.id == step_id), None)
                if step is not None and step.retries < MAX_STEP_RETRIES:
                    return step
        return None

    def back_to_step(self, step: StepState) -> None:
        self.out.steps.append({"op": "update", "key": step.key, "fields": {"status": "running", "retries": step.retries + 1}})
        self.out.run["plan"] = {**self.s.plan, "current": step.key}
        self.goto("execute")

    # -- reflect stage ----------------------------------------------------------------------
    def do_submit_reflection(self, request: dict) -> None:
        try:
            reflection = Reflection.model_validate(request["arguments"])
            problems = self.reflection_problems(reflection)
        except ValidationError as exc:
            reflection, problems = None, [f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors(include_input=False)]
        rounds = self.counters.get("reflectRounds", 0) + 1
        if problems:
            invalid = self.counters.get("reflectionInvalid", 0) + 1
            self.counters["reflectionInvalid"] = invalid
            self.reject(request, "REFLECTION_INVALID", "复盘结论无效：" + "；".join(problems[:5]))
            if invalid < 2:
                return
            # Two invalid reviews in a row: keep the guard rail and ask for a rewrite instead of guessing accept.
            reflection = Reflection(verdict="revise_answer", issues=[
                {"kind": "incomplete", "problem": "复盘输出无效，保守地要求改写回答", "stepKey": None, "evidenceRefs": []}])
        self.counters["reflectionInvalid"] = 0
        self.counters["reflectRounds"] = rounds
        draft = dict(self.s.draft or {})
        self.out.reflections.append({"source": "model", "verdict": reflection.verdict, "question": reflection.question,
            "hard_checks": draft.get("verification"), "issues": [i.model_dump(mode="json") for i in reflection.issues]})
        self.out.events.append("reflection.recorded")
        draft["reflection"] = {"verdict": reflection.verdict, "round": rounds,
                               "issues": [i.model_dump(mode="json") for i in reflection.issues]}
        self.out.run["claims"] = draft
        if not any(p.request["id"] == request["id"] for p in self.out.plans):
            self.add(control_plan(request, _receipt(request, accepted=True, effect={"verdict": reflection.verdict})))
        verdict, answer = reflection.verdict, draft.get("answer")
        if verdict == "accept":
            return self.finish("completed", None, answer)
        if verdict == "ask_user":
            self.out.run["waiting"] = {"question": reflection.question, "stage": "reflect"}
            self.out.run["stage"] = "waiting"
            return self.finish("waiting_input", None)
        if rounds >= MAX_REFLECT_ROUNDS:
            return self.finish("limit_reached", error("REFLECTION_NOT_ACCEPTED", "多次复盘后回答仍未被接受"), answer)
        if verdict == "replan":
            if self.bump("planRevisions", MAX_PLAN_REVISIONS):
                return self.finish("limit_reached", error("PLAN_REVISIONS_EXHAUSTED", "多次重新规划后仍无法完成"), answer)
            return self.goto("plan")
        if verdict == "retry_step":
            key = next(i.stepKey for i in reflection.issues if i.stepKey)
            step = self.s.step_by_key(key)
            if step.retries >= MAX_STEP_RETRIES:
                return self.finish("limit_reached", error("STEP_RETRIES_EXHAUSTED", "步骤重试次数已用尽"), answer)
            return self.back_to_step(step)
        if self.bump("answerRevisions", pol.MAX_ANSWER_REVISIONS):
            return self.finish("limit_reached", error("REFLECTION_NOT_ACCEPTED", "回答多次未被复盘接受"), answer)
        self.goto("respond")

    def reflection_problems(self, reflection: Reflection) -> list[str]:
        problems, index = [], self.index()
        keys = {step.key for step in self.s.steps}
        for number, issue in enumerate(reflection.issues, 1):
            if not issue.evidenceRefs and not issue.stepKey:
                problems.append(f"问题 {number} 没有 evidenceRefs 或 stepKey")
            problems.extend(f"问题 {number} 引用的 {ref} 不存在" for ref in issue.evidenceRefs
                            if verifier.resolve_ref(index, ref) is None)
            if issue.stepKey and issue.stepKey not in keys:
                problems.append(f"问题 {number} 的 stepKey {issue.stepKey} 不存在")
        if reflection.verdict == "accept" and reflection.issues:
            problems.append("accept 时 issues 必须为空")
        if reflection.verdict == "ask_user" and not reflection.question:
            problems.append("ask_user 必须给出 question")
        if reflection.verdict == "retry_step" and not any(i.stepKey for i in reflection.issues):
            problems.append("retry_step 必须在 issue 中给出 stepKey")
        return problems
