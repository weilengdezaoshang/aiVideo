"""创作 Agent 流程评测（L0–L5、L7）。

沿用现有用例、runner、类型化规则与报告。执行通过 Node 22 调用
apps/server 的 Memory composition root，读取真实 Trace v1 文件再判分。
不调用付费模型，不用固定期望冒充运行结果。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .graders.rules import Answer

FLOW_SUITES = (
    "agent_flow",
    "L0",
    "L1",
    "L2",
    "L3",
    "L4",
    "L5",
    "L7",
    "protocol",
    "routing",
    "plan",
    "interrupt",
    "continue",
    "queue",
    "reflection",
)
LAYERS = ("L0", "L1", "L2", "L3", "L4", "L5", "L7")
DATASET_FILES = (
    "agent-flow-l0-protocol.jsonl",
    "agent-flow-l1-routing.jsonl",
    "agent-flow-l2-plan.jsonl",
    "agent-flow-l3-interrupt.jsonl",
    "agent-flow-l4-continue.jsonl",
    "agent-flow-l5-queue.jsonl",
    "agent-flow-l7-reflection.jsonl",
)
DRIVER_ERROR = "BullMQ/Redis 为显式未实现的扩展骨架；本地请使用 memory 异步队列，不能静默回退"
QUEUE_FULL = "队列已满，请稍后重试"
ACTIONS = {"send", "resume", "stop", "invalid", "resume_unknown", "stop_unknown", "queue"}
PROBES = {
    "rate_limit",
    "concurrency",
    "idempotency",
    "cancel",
    "timeout",
    "backpressure",
    "expiry",
    "bullmq",
}
ROUTES = {"direct", "handoff_planner", "handoff_roundtrip"}
TERMINALS = {"stream_complete", "stream_error", "interrupt_approval"}
OUTCOMES = {"success", "failed", "cancelled", "interrupted"}

_TOP_TYPES = {
    "sseStart": "bool",
    "sseMonotonic": "bool",
    "terminal": "str",
    "hasConversationId": "bool",
    "hasTurnId": "bool",
    "traceV1": "bool",
    "outcome": "str",
    "route": "str",
    "mustAskUser": "bool",
    "noHandoff": "bool",
    "toolsAny": "str_list",
    "maxSteps": "int",
    "maxCredits": "int",
    "httpStatus": "int",
    "errorProtocol": "bool",
    "planUnique": "bool",
    "planAdvanced": "bool",
    "planCompleted": "bool",
    "planStepCount": "int",
    "planIdsStable": "bool",
    "completedKept": "bool",
    "successArtifacts": "int",
    "duplicateIdempotent": "bool",
    "conflictStatus": "int",
    "draftCleared": "bool",
    "admissionVerdict": "str",
    "admissionLegal": "bool",
    "admissionTypes": "bool",
    "admissionIntent": "bool",
    "reviewOutcome": "str",
    "reviewAttempts": "int",
    "reviewRun": "bool",
    "escalationText": "bool",
}
_CANCEL = {"newArtifacts": "int", "newCredits": "int"}
_APPROVE = {"minArtifacts": "int", "minCredits": "int"}
_CONTINUE = {
    "hasPlan": "bool",
    "cursorKept": "bool",
    "completedKept": "bool",
    "notRedone": "bool",
    "minCompletedBeforeStop": "int",
}
_QUEUE = {
    "rateLimit429": "bool",
    "hasRetryAfter": "bool",
    "concurrencyRejected": "bool",
    "singleExecution": "bool",
    "cancelStatus": "str",
    "timeoutStatus": "str",
    "backpressure": "bool",
    "expired": "bool",
    "bullmqRefused": "bool",
}


class FlowHarnessError(RuntimeError):
    """Node 适配器没有产出可判分的真实观测。"""


def dataset_dir(root: Path) -> Path:
    return root / "evals" / "datasets"


def dataset_files(root: Path) -> list[Path]:
    directory = dataset_dir(root)
    missing = [name for name in DATASET_FILES if not (directory / name).is_file()]
    if missing:
        raise FileNotFoundError("缺少 agent_flow 数据集:" + ",".join(missing))
    return [directory / name for name in DATASET_FILES]


def case_layer(suites: list[str]) -> str:
    for layer in LAYERS:
        if layer in suites:
            return layer
    return "agent_flow"


def _kind(value: Any, kind: str, label: str) -> None:
    if kind == "bool" and not isinstance(value, bool):
        raise ValueError(f"{label} 必须是布尔值")
    if kind == "int" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValueError(f"{label} 必须是整数")
    if kind == "str" and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{label} 必须是非空字符串")
    if kind == "str_list":
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            raise ValueError(f"{label} 必须是非空字符串数组")


def _validate_expect(expect: Any) -> dict:
    if not isinstance(expect, dict) or not expect:
        raise ValueError("agent_flow 用例必须包含非空 expect")
    unknown = set(expect) - set(_TOP_TYPES) - {"afterCancel", "afterApprove", "afterContinue", "queue"}
    if unknown:
        raise ValueError(f"agent_flow expect 含未知字段 {sorted(unknown)}")
    for key, kind in _TOP_TYPES.items():
        if key in expect:
            _kind(expect[key], kind, f"expect.{key}")
    if "route" in expect and expect["route"] not in ROUTES:
        raise ValueError(f"expect.route 不支持:{expect['route']}")
    if "terminal" in expect and expect["terminal"] not in TERMINALS:
        raise ValueError(f"expect.terminal 不支持:{expect['terminal']}")
    if "outcome" in expect and expect["outcome"] not in OUTCOMES:
        raise ValueError(f"expect.outcome 不支持:{expect['outcome']}")
    for name, spec in (
        ("afterCancel", _CANCEL),
        ("afterApprove", _APPROVE),
        ("afterContinue", _CONTINUE),
        ("queue", _QUEUE),
    ):
        if name not in expect:
            continue
        nested = expect[name]
        if not isinstance(nested, dict) or not nested:
            raise ValueError(f"expect.{name} 必须是非空对象")
        extra = set(nested) - set(spec)
        if extra:
            raise ValueError(f"expect.{name} 含未知字段 {sorted(extra)}")
        for key, kind in spec.items():
            if key in nested:
                _kind(nested[key], kind, f"expect.{name}.{key}")
    return expect


def _validate_steps(steps: Any) -> list:
    if not isinstance(steps, list) or not steps:
        raise ValueError("agent_flow 用例必须包含非空 steps")
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError(f"steps[{index}] 必须是对象")
        action = step.get("action")
        if action not in ACTIONS:
            raise ValueError(f"steps[{index}] 不支持的 action:{action}")
        extra = set(step) - {"action", "prompt", "approved", "body", "when", "probe"}
        if extra:
            raise ValueError(f"steps[{index}] 未知字段 {sorted(extra)}")
        if action == "send" and (not isinstance(step.get("prompt"), str) or not step["prompt"].strip()):
            raise ValueError(f"steps[{index}] send 需要非空 prompt")
        if action == "resume" and not isinstance(step.get("approved"), bool):
            raise ValueError(f"steps[{index}] resume 需要布尔 approved")
        if action == "queue" and step.get("probe") not in PROBES:
            raise ValueError(f"steps[{index}] queue probe 不支持:{step.get('probe')}")
        if action == "stop" and step.get("when") not in (None, "generating"):
            raise ValueError(f"steps[{index}] stop.when 只允许 generating")
    return steps


def _validate_mocks(mocks: Any) -> dict:
    if mocks is None:
        return {"imageProvider": "stub"}
    if not isinstance(mocks, dict):
        raise ValueError("mocks 必须是对象")
    extra = set(mocks) - {"imageProvider", "toolDelayMs", "imageCredits", "creditThreshold", "reviewEnabled", "reviewFixture", "maxReviewRetry"}
    if extra:
        raise ValueError(f"mocks 含未知字段 {sorted(extra)}")
    provider = mocks.get("imageProvider", "stub")
    if provider != "stub":
        raise ValueError("mocks.imageProvider 只允许 stub，禁止真实付费模型")
    for key in ("toolDelayMs", "imageCredits", "creditThreshold"):
        if key in mocks and (isinstance(mocks[key], bool) or not isinstance(mocks[key], int) or mocks[key] < 0):
            raise ValueError(f"mocks.{key} 必须是非负整数")
    if "reviewEnabled" in mocks and not isinstance(mocks["reviewEnabled"], bool):
        raise ValueError("reviewEnabled 必须是布尔值")
    if mocks.get("reviewFixture", "good") not in {"good", "bad_once", "bad"}:
        raise ValueError("reviewFixture 必须为 good/bad_once/bad")
    if "maxReviewRetry" in mocks:
        _kind(mocks["maxReviewRetry"], "int", "maxReviewRetry")
        if mocks["maxReviewRetry"] < 0:
            raise ValueError("maxReviewRetry 必须是非负整数")
    return mocks


def _check(check_id: str, kind: str, question: str, expected: Any) -> dict:
    return {
        "id": check_id,
        "kind": kind,
        "question": question,
        "expected": expected,
        "required": True,
        "weight": 1.0,
        "dependsOn": [],
    }


def _tool_check_id(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in name.lower())
    ident = f"tool_{cleaned}"
    if len(ident) < 3 or len(ident) > 64:
        raise ValueError(f"工具名无法作为检查项 ID:{name}")
    return ident


def checks_for_expect(expect: dict) -> list[dict]:
    """把 expect 收成现有类型化检查项。reason 码即检查项 ID。"""
    checks: list[dict] = []
    from .plan_admission import admission_check_id
    for key in ("admissionVerdict", "admissionLegal", "admissionTypes", "admissionIntent"):
        if key in expect:
            checks.append(_check(admission_check_id(key), "text" if key == "admissionVerdict" else "boolean",
                f"执行准入 {key} 是否符合预期?", expect[key]))
    for key, check_id, kind in [("reviewOutcome", "review_outcome", "text"), ("reviewAttempts", "unexpected_retry", "integer"), ("reviewRun", "review_not_run", "boolean"), ("escalationText", "missing_escalation", "boolean")]:
        if key in expect:
            checks.append(_check(check_id, kind, f"Reflection {key} 是否符合预期?", expect[key]))
    if "sseStart" in expect:
        checks.append(_check("sse_start", "boolean", "SSE 是否以 stream_start 开始?", expect["sseStart"]))
    if "sseMonotonic" in expect:
        checks.append(_check("sse_monotonic", "boolean", "SSE sequence 是否严格递增?", expect["sseMonotonic"]))
    if "terminal" in expect:
        checks.append(_check("sse_terminal", "text", "SSE 终态事件类型是什么?", expect["terminal"]))
    if "hasConversationId" in expect:
        checks.append(_check("conversation_id", "boolean", "事件是否带 conversation_id?", expect["hasConversationId"]))
    if "hasTurnId" in expect:
        checks.append(_check("turn_id", "boolean", "事件是否带 turn_uuid?", expect["hasTurnId"]))
    if "traceV1" in expect:
        checks.append(_check("trace_v1", "boolean", "是否写出并通过 Trace v1 校验?", expect["traceV1"]))
    if "outcome" in expect:
        checks.append(_check("outcome", "text", "最后一条 Trace 的 outcome 是什么?", expect["outcome"]))
    if "route" in expect:
        checks.append(_check("route", "text", "实际路由是什么?", expect["route"]))
    if "mustAskUser" in expect:
        checks.append(_check("must_ask_user", "boolean", "是否出现积分确认?", expect["mustAskUser"]))
    if "noHandoff" in expect:
        checks.append(_check("no_handoff", "boolean", "是否没有 handoff?", expect["noHandoff"]))
    for name in expect.get("toolsAny") or []:
        checks.append(_check(_tool_check_id(name), "boolean", f"是否调用了工具 {name}?", True))
    if "maxSteps" in expect:
        checks.append(_check("steps_within_max", "boolean", f"步骤数是否不超过 {expect['maxSteps']}?", True))
    if "maxCredits" in expect:
        checks.append(_check("credits_within_max", "boolean", f"积分是否不超过 {expect['maxCredits']}?", True))
    if "httpStatus" in expect:
        checks.append(_check("http_status", "integer", "HTTP 状态码是多少?", expect["httpStatus"]))
    if "errorProtocol" in expect:
        checks.append(_check("error_protocol", "boolean", "错误响应是否包含 error 字符串?", expect["errorProtocol"]))
    if "planUnique" in expect:
        checks.append(_check("plan_unique", "boolean", "同一条助手消息是否只有一份计划?", expect["planUnique"]))
    if "planAdvanced" in expect:
        checks.append(_check("plan_advanced", "boolean", "计划状态是否向前推进?", expect["planAdvanced"]))
    if "planCompleted" in expect:
        checks.append(_check("plan_completed", "boolean", "计划步骤是否全部完成?", expect["planCompleted"]))
    if "planStepCount" in expect:
        checks.append(_check("plan_step_count", "integer", "计划有几步?", expect["planStepCount"]))
    if "planIdsStable" in expect:
        checks.append(_check("plan_ids_stable", "boolean", "计划步骤 ID 是否保持不变?", expect["planIdsStable"]))
    if "completedKept" in expect:
        checks.append(_check("completed_kept", "boolean", "已完成步骤是否没有回退?", expect["completedKept"]))
    if "successArtifacts" in expect:
        checks.append(_check("success_artifacts", "integer", "成功作品有几份?", expect["successArtifacts"]))
    cancel = expect.get("afterCancel") or {}
    if "newArtifacts" in cancel:
        checks.append(_check("cancel_new_artifacts", "integer", "取消后新增成功作品有几份?", cancel["newArtifacts"]))
    if "newCredits" in cancel:
        checks.append(_check("cancel_new_credits", "integer", "取消后新增积分是多少?", cancel["newCredits"]))
    approve = expect.get("afterApprove") or {}
    if "minArtifacts" in approve:
        checks.append(_check("approve_min_artifacts", "boolean", f"批准后成功作品是否不少于 {approve['minArtifacts']}?", True))
    if "minCredits" in approve:
        checks.append(_check("approve_min_credits", "boolean", f"批准后积分是否不少于 {approve['minCredits']}?", True))
    if "duplicateIdempotent" in expect:
        checks.append(_check("duplicate_idempotent", "boolean", "重复确认是否没有重跑生成?", expect["duplicateIdempotent"]))
    if "conflictStatus" in expect:
        checks.append(_check("confirm_conflict", "integer", "相反确认的 HTTP 状态是多少?", expect["conflictStatus"]))
    follow = expect.get("afterContinue") or {}
    if "hasPlan" in follow:
        checks.append(_check("continue_has_plan", "boolean", "继续后是否重新展示计划?", follow["hasPlan"]))
    if "cursorKept" in follow:
        checks.append(_check("continue_cursor_kept", "boolean", "继续是否保留执行游标与步骤 ID?", follow["cursorKept"]))
    if "completedKept" in follow:
        checks.append(_check("continue_completed_kept", "boolean", "继续是否保留已完成步骤?", follow["completedKept"]))
    if "notRedone" in follow:
        checks.append(_check("continue_not_redone", "boolean", "继续是否没有重做已完成步骤?", follow["notRedone"]))
    if "minCompletedBeforeStop" in follow:
        checks.append(
            _check(
                "continue_min_completed",
                "boolean",
                f"停止前已完成步骤是否不少于 {follow['minCompletedBeforeStop']}?",
                True,
            )
        )
    if "draftCleared" in expect:
        checks.append(_check("draft_cleared", "boolean", "新需求是否清除了 continuation draft?", expect["draftCleared"]))
    queue = expect.get("queue") or {}
    if "rateLimit429" in queue:
        checks.append(_check("rate_limit_429", "boolean", "超限请求是否返回 429?", queue["rateLimit429"]))
    if "hasRetryAfter" in queue:
        checks.append(_check("retry_after", "boolean", "429 是否带 Retry-After?", queue["hasRetryAfter"]))
    if "concurrencyRejected" in queue:
        checks.append(_check("concurrency_cap", "boolean", "超过并发上限是否返回 429?", queue["concurrencyRejected"]))
    if "singleExecution" in queue:
        checks.append(_check("queue_idempotent", "boolean", "相同幂等键是否只执行一次?", queue["singleExecution"]))
    if "cancelStatus" in queue:
        checks.append(_check("queue_cancel", "text", "取消后的队列状态是什么?", queue["cancelStatus"]))
    if "timeoutStatus" in queue:
        checks.append(_check("queue_timeout", "text", "超时后的队列状态是什么?", queue["timeoutStatus"]))
    if "backpressure" in queue:
        checks.append(_check("queue_backpressure", "boolean", "队列满时是否拒绝新任务?", queue["backpressure"]))
    if "expired" in queue:
        checks.append(_check("queue_expiry", "boolean", "过期收据是否被回收且同一键可重新执行?", queue["expired"]))
    if "bullmqRefused" in queue:
        checks.append(_check("bullmq_refused", "boolean", "BullMQ/Redis 驱动是否被明确拒绝?", queue["bullmqRefused"]))
    if not checks:
        raise ValueError("agent_flow expect 没有可执行的检查项")
    return checks


def _first_prompt(steps: list) -> str:
    for step in steps:
        if step.get("action") == "send" and isinstance(step.get("prompt"), str):
            return step["prompt"].strip()
    return ""


def normalize_agent_flow_payload(data: dict) -> dict:
    """源 JSONL 或已冻结快照都归一成 CaseVersion 可接受的字典。"""
    data = dict(data)
    raw_input = data.get("input") if isinstance(data.get("input"), dict) else {}
    params = dict(raw_input.get("params") or {})
    for key in ("steps", "expect", "mocks"):
        if key in data and data[key] is not None:
            params[key] = data[key]
    already = (
        params.get("kind") == "agent_flow"
        and data.get("checks")
        and "steps" not in data
        and "expect" not in data
    )
    steps = _validate_steps(params.get("steps"))
    expect = _validate_expect(params.get("expect"))
    mocks = _validate_mocks(params.get("mocks") or {"imageProvider": "stub"})
    if already:
        data["input"] = {
            **raw_input,
            "params": {**params, "steps": steps, "expect": expect, "mocks": mocks, "kind": "agent_flow"},
        }
        return data
    prompt = raw_input.get("prompt") if isinstance(raw_input.get("prompt"), str) else ""
    prompt = prompt.strip() or _first_prompt(steps) or str(data.get("caseId") or "agent-flow")
    data["input"] = {
        "prompt": prompt,
        "referenceArtifactIds": list(raw_input.get("referenceArtifactIds") or []),
        "params": {"kind": "agent_flow", "steps": steps, "expect": expect, "mocks": mocks,
            **({"admission": params["admission"]} if "admission" in params else {})},
    }
    if not data.get("checks"):
        data["checks"] = checks_for_expect(expect)
    for key in ("steps", "expect", "mocks"):
        data.pop(key, None)
    return data


def filter_cases_by_suite(cases: list, suite: str) -> list:
    from typing import get_args

    from .models import SuiteType

    allowed = set(get_args(SuiteType))
    if suite not in allowed:
        raise ValueError(f"不支持的 suite:{suite}。可选:{', '.join(sorted(allowed))}")
    selected = [case for case in cases if suite in case.suites]
    if not selected:
        raise ValueError(f"suite {suite} 没有用例")
    return selected


def select_flow_suite(cases: list, suite: str) -> list:
    if suite not in FLOW_SUITES:
        raise ValueError(f"不支持的 suite:{suite}。可选:{', '.join(FLOW_SUITES)}")
    return filter_cases_by_suite(cases, suite)


def load_flow_sources(root: Path) -> list[tuple[dict, Any]]:
    from .datasets import read_jsonl
    from .models import CaseVersion

    loaded: list[tuple[dict, Any]] = []
    seen: set[str] = set()
    for path in dataset_files(root):
        for item in read_jsonl(path):
            case = CaseVersion.model_validate(item)
            if case.caseId in seen:
                raise ValueError(f"caseId 重复:{case.caseId}")
            seen.add(case.caseId)
            loaded.append((item, case))
    if not loaded:
        raise ValueError("agent_flow 数据集没有任何用例")
    return loaded


def resolve_node(explicit: str | None = None) -> str:
    if explicit:
        if not Path(explicit).is_file():
            raise FlowHarnessError(f"找不到 Node:{explicit}")
        return explicit
    env = os.environ.get("AIVIDEO_NODE")
    if env:
        if not Path(env).is_file():
            raise FlowHarnessError(f"AIVIDEO_NODE 不可执行:{env}")
        return env
    candidate = Path("/Users/weilengdezaoshang/.nvm/versions/node/v22.23.2/bin/node")
    if candidate.is_file():
        return str(candidate)
    return "node"


def execute_flow_case(root: Path, case: Any, out_dir: Path, node: str | None = None) -> dict:
    """跑一条用例。适配器只收到 steps/mocks，不接收 expect。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    params = case.input.params
    if "admission" in params:
        from .plan_admission import execute_admission_case
        return execute_admission_case(params["admission"], out_dir)
    payload = {"caseId": case.caseId, "steps": params["steps"], "mocks": params.get("mocks") or {"imageProvider": "stub"}}
    case_path = out_dir / "case.json"
    case_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    script = root / "apps" / "server" / "src" / "eval" / "run-flow-case.ts"
    if not script.is_file():
        raise FlowHarnessError(f"缺少 Agent 适配器:{script}")
    binary = resolve_node(node)
    try:
        proc = subprocess.run(
            [binary, "--import", "tsx", str(script), "--case", str(case_path), "--out", str(out_dir)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FlowHarnessError(f"{case.caseId} 执行超时") from exc
    observation_path = out_dir / "observation.json"
    if proc.returncode != 0 or not observation_path.is_file():
        detail = (proc.stderr or proc.stdout or "无输出")[-800:]
        raise FlowHarnessError(f"{case.caseId} 执行失败:{detail}")
    try:
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise FlowHarnessError(f"{case.caseId} 观测不是合法 JSON") from exc
    if not isinstance(observation, dict):
        raise FlowHarnessError(f"{case.caseId} 观测必须是对象")
    return observation


def _events(observation: dict) -> list[dict]:
    events: list[dict] = []
    for turn in observation.get("turns") or []:
        if isinstance(turn, dict):
            events.extend(event for event in turn.get("events") or [] if isinstance(event, dict))
    return events


def _plan_snapshots(events: list[dict]) -> list[list[dict]]:
    snapshots: list[list[dict]] = []
    for event in events:
        if event.get("kind") == "system" and event.get("type") == "todo_plan":
            steps = event.get("steps") or []
            if isinstance(steps, list):
                snapshots.append([step for step in steps if isinstance(step, dict)])
        message = event.get("message") if event.get("kind") == "message" else None
        if isinstance(message, dict):
            for part in message.get("parts") or []:
                if isinstance(part, dict) and part.get("kind") == "plan" and isinstance(part.get("steps"), list):
                    snapshots.append([step for step in part["steps"] if isinstance(step, dict)])
    return snapshots


def _last_message(events: list[dict]) -> dict | None:
    found = None
    for event in events:
        if event.get("kind") == "message" and isinstance(event.get("message"), dict):
            found = event["message"]
    return found


def _artifact_count(message: dict | None) -> int:
    if not message:
        return 0
    count = 0
    for part in message.get("parts") or []:
        if isinstance(part, dict) and part.get("kind") == "artifact" and part.get("status") == "success":
            count += 1
    return count


def _plan_part_count(message: dict | None) -> int:
    if not message:
        return 0
    return sum(1 for part in message.get("parts") or [] if isinstance(part, dict) and part.get("kind") == "plan")


def _tool_names(events: list[dict], traces: list[dict]) -> set[str]:
    names: set[str] = set()
    for event in events:
        message = event.get("message") if isinstance(event.get("message"), dict) else None
        for part in (message or {}).get("parts") or []:
            if isinstance(part, dict) and part.get("kind") == "tool_status" and isinstance(part.get("toolName"), str):
                names.add(part["toolName"])
    for trace in traces:
        for turn in trace.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            for call in turn.get("toolCalls") or []:
                if isinstance(call, dict) and isinstance(call.get("name"), str):
                    names.add(call["name"])
    return names


def _load_traces(observation: dict) -> tuple[list[str], list[dict]]:
    paths: list[str] = []
    documents: list[dict] = []
    raw_paths = list(observation.get("traces") or [])
    for turn in observation.get("turns") or []:
        if isinstance(turn, dict) and turn.get("tracePath"):
            raw_paths.append(turn["tracePath"])
    seen: set[str] = set()
    for raw in raw_paths:
        if not isinstance(raw, str) or raw in seen:
            continue
        seen.add(raw)
        path = Path(raw)
        if not path.is_file():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(document, dict):
            paths.append(str(path.resolve()))
            documents.append(document)
    return paths, documents


def _trace_ok(document: dict) -> bool:
    if document.get("version") != "1":
        return False
    if not isinstance(document.get("runId"), str) or not document["runId"]:
        return False
    if not isinstance(document.get("conversationId"), str) or not document["conversationId"]:
        return False
    if not isinstance(document.get("startedAt"), str) or not isinstance(document.get("finishedAt"), str):
        return False
    turns = document.get("turns")
    if not isinstance(turns, list) or not turns:
        return False
    for turn in turns:
        if not isinstance(turn, dict):
            return False
        if not isinstance(turn.get("turnId"), str) or turn.get("outcome") not in OUTCOMES:
            return False
        metrics = turn.get("metrics")
        if not isinstance(metrics, dict):
            return False
        for key in ("steps", "credits", "latencyMs"):
            if isinstance(metrics.get(key), bool) or not isinstance(metrics.get(key), (int, float)):
                return False
        if not isinstance(turn.get("toolCalls"), list) or not isinstance(turn.get("events"), list):
            return False
        if not isinstance(turn.get("interrupts"), list) or not isinstance(turn.get("agents"), list):
            return False
    return True


def _route(names: set[str]) -> str:
    if "handoff_to_planner" in names and "handoff_to_host" in names:
        return "handoff_roundtrip"
    if "handoff_to_planner" in names:
        return "handoff_planner"
    return "direct"


def _metric_sum(traces: list[dict], key: str) -> int | None:
    if not traces:
        return None
    total = 0
    for document in traces:
        for turn in document.get("turns") or []:
            metrics = turn.get("metrics") if isinstance(turn, dict) else None
            if not isinstance(metrics, dict) or isinstance(metrics.get(key), bool):
                return None
            if not isinstance(metrics.get(key), (int, float)):
                return None
            total += int(metrics[key])
    return total


def _status_rank(status: str) -> int | None:
    return {"pending": 0, "in_progress": 1, "completed": 2}.get(status)


def _advanced(snapshots: list[list[dict]]) -> bool:
    seen: dict[str, int] = {}
    for snapshot in snapshots:
        for step in snapshot:
            ident = step.get("id")
            rank = _status_rank(str(step.get("status")))
            if not isinstance(ident, str) or rank is None:
                continue
            previous = seen.get(ident)
            if previous is not None and rank > previous:
                return True
            seen[ident] = rank if previous is None else max(previous, rank)
    return False


def _completed_kept(snapshots: list[list[dict]]) -> bool:
    done: set[str] = set()
    for snapshot in snapshots:
        current = {step.get("id"): step.get("status") for step in snapshot}
        for ident in done:
            if current.get(ident) != "completed":
                return False
        done.update(ident for ident, status in current.items() if status == "completed" and isinstance(ident, str))
    return True


def _ids_stable(snapshots: list[list[dict]]) -> bool:
    lists = [[step.get("id") for step in snapshot] for snapshot in snapshots]
    return bool(lists) and all(item == lists[0] for item in lists)


def _continue_pair(observation: dict) -> tuple[dict | None, dict | None]:
    draft_turn = None
    continue_turn = None
    for turn in observation.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        measured = turn.get("measured") if isinstance(turn.get("measured"), dict) else {}
        if measured.get("draftPresent") and measured.get("draftStepIds"):
            draft_turn = turn
        prompt = ""
        if turn.get("action") == "send":
            prompt = str(turn.get("prompt") or "")
        if prompt.strip() in {"继续", "接着做", "接着", "继续执行", "继续生成", "继续上次", "resume"} or prompt.strip().lower() == "resume":
            continue_turn = turn
    return draft_turn, continue_turn


def _answer(check_id: str, observed: Any, evidence: str) -> Answer:
    return Answer(checkId=check_id, observed=observed, evidence=evidence, source="rules")


def observe_agent_flow(case: Any, observation: dict) -> tuple[dict[str, Answer], list[str]]:
    """从 SSE、会话测量和磁盘上的 Trace v1 得到检查项回答。"""
    expect = case.input.params.get("expect") or {}
    events = _events(observation)
    trace_paths, traces = _load_traces(observation)
    names = _tool_names(events, traces)
    snapshots = _plan_snapshots(events)
    turns = [turn for turn in observation.get("turns") or [] if isinstance(turn, dict)]
    last_turn = turns[-1] if turns else {}
    last_events = [event for event in (last_turn.get("events") or []) if isinstance(event, dict)]
    http_events = events or last_events
    queue = observation.get("queue") if isinstance(observation.get("queue"), dict) else {}
    evidence = f"reason={{id}}; layer={case_layer(list(case.suites))}; trace={trace_paths[-1] if trace_paths else '无'}"

    def put(check_id: str, observed: Any) -> None:
        answers[check_id] = _answer(check_id, observed, evidence.replace("{id}", check_id))

    answers: dict[str, Answer] = {}
    from .plan_admission import review_observations, admission_check_id
    for key, value in review_observations(observation.get("planReview") or {}).items():
        if key in expect:
            put(admission_check_id(key), value)
    generation_calls = [call for document in traces for trace_turn in document.get("turns", []) for call in trace_turn.get("toolCalls", []) if call.get("name") == "creative_agent_mcp_gen_text2image_v3"]
    latest = generation_calls[-1] if generation_calls else {}
    if "reviewOutcome" in expect:
        put("review_outcome", latest.get("reviewOutcome", "not_run"))
    if "reviewAttempts" in expect:
        put("unexpected_retry", latest.get("reviewAttempts", 0))
    if "reviewRun" in expect:
        put("review_not_run", bool((latest.get("evidence") or {}).get("review")))
    if "escalationText" in expect:
        parts = (_last_message(last_events or events) or {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts if part.get("kind") == "text")
        issues = ((latest.get("evidence") or {}).get("review") or {}).get("issues", [])
        put("missing_escalation", latest.get("reviewOutcome") == "escalated" and bool(issues) and "未通过质量检查" in text and all(issue in text for issue in issues))
    if "sseStart" in expect:
        put("sse_start", bool(http_events) and http_events[0].get("kind") == "system" and http_events[0].get("type") == "stream_start")
    if "sseMonotonic" in expect:
        sequences = [event.get("sequence") for event in http_events]
        put(
            "sse_monotonic",
            bool(sequences)
            and all(isinstance(item, int) and not isinstance(item, bool) for item in sequences)
            and sequences == sorted(set(sequences)),
        )
    if "terminal" in expect:
        terminals = [event for event in http_events if event.get("kind") == "system"]
        put("sse_terminal", str(terminals[-1].get("type")) if terminals else "")
    if "hasConversationId" in expect:
        put("conversation_id", bool(http_events) and all(isinstance(event.get("conversation_id"), str) and event.get("conversation_id") for event in http_events))
    if "hasTurnId" in expect:
        put("turn_id", bool(http_events) and all(isinstance(event.get("turn_uuid"), str) and event.get("turn_uuid") for event in http_events))
    if "traceV1" in expect:
        put("trace_v1", bool(traces) and all(_trace_ok(document) for document in traces))
    if "outcome" in expect:
        outcome = ""
        if traces:
            last_turns = traces[-1].get("turns") or []
            if last_turns and isinstance(last_turns[-1], dict):
                outcome = str(last_turns[-1].get("outcome") or "")
        put("outcome", outcome)
    if "route" in expect:
        put("route", _route(names))
    if "mustAskUser" in expect:
        put("must_ask_user", any(event.get("kind") == "system" and event.get("type") == "interrupt_approval" for event in events))
    if "noHandoff" in expect:
        put("no_handoff", "handoff_to_planner" not in names and "handoff_to_host" not in names)
    for name in expect.get("toolsAny") or []:
        put(_tool_check_id(name), name in names)
    if "maxSteps" in expect:
        total = _metric_sum(traces, "steps")
        put("steps_within_max", total is not None and total <= int(expect["maxSteps"]))
    if "maxCredits" in expect:
        total = _metric_sum(traces, "credits")
        put("credits_within_max", total is not None and total <= int(expect["maxCredits"]))
    if "httpStatus" in expect:
        put("http_status", int(last_turn.get("httpStatus")) if isinstance(last_turn.get("httpStatus"), int) else -1)
    if "errorProtocol" in expect:
        body = last_turn.get("errorBody") if isinstance(last_turn.get("errorBody"), dict) else {}
        put("error_protocol", isinstance(body.get("error"), str) and bool(body.get("error")))
    if "planUnique" in expect:
        messages = []
        for event in events:
            if event.get("kind") == "message" and isinstance(event.get("message"), dict):
                messages.append(event["message"])
        put("plan_unique", any(_plan_part_count(message) == 1 for message in messages) and all(_plan_part_count(message) <= 1 for message in messages))
    if "planAdvanced" in expect:
        put("plan_advanced", _advanced(snapshots))
    if "planCompleted" in expect:
        last = snapshots[-1] if snapshots else []
        put("plan_completed", bool(last) and all(step.get("status") == "completed" for step in last))
    if "planStepCount" in expect:
        last = snapshots[-1] if snapshots else []
        put("plan_step_count", len(last))
    if "planIdsStable" in expect:
        put("plan_ids_stable", _ids_stable(snapshots))
    if "completedKept" in expect:
        put("completed_kept", bool(snapshots) and _completed_kept(snapshots))
    if "successArtifacts" in expect:
        put("success_artifacts", _artifact_count(_last_message(last_events or events)))
    cancel = expect.get("afterCancel") or {}
    if cancel:
        cancel_turn = next((turn for turn in turns if turn.get("action") == "resume" and turn.get("approved") is False), last_turn)
        cancel_events = [event for event in (cancel_turn.get("events") or []) if isinstance(event, dict)]
        if "newArtifacts" in cancel:
            put("cancel_new_artifacts", _artifact_count(_last_message(cancel_events)))
        if "newCredits" in cancel:
            # 取消写回同一 run 的 Trace，积分以该文件为准，不用期望值填充。
            path = cancel_turn.get("tracePath")
            document = next((item for item_path, item in zip(trace_paths, traces) if path and item_path == str(Path(path).resolve())), None)
            credits = None
            if document:
                metrics = ((document.get("turns") or [{}])[-1] or {}).get("metrics") or {}
                if isinstance(metrics.get("credits"), (int, float)) and not isinstance(metrics.get("credits"), bool):
                    credits = int(metrics["credits"])
            put("cancel_new_credits", -1 if credits is None else credits)
    approve = expect.get("afterApprove") or {}
    if approve:
        approve_turn = next((turn for turn in reversed(turns) if turn.get("action") == "resume" and turn.get("approved") is True), last_turn)
        approve_events = [event for event in (approve_turn.get("events") or []) if isinstance(event, dict)]
        artifacts = _artifact_count(_last_message(approve_events))
        path = approve_turn.get("tracePath")
        document = next((item for item_path, item in zip(trace_paths, traces) if path and item_path == str(Path(path).resolve())), None)
        credits = None
        if document:
            metrics = ((document.get("turns") or [{}])[-1] or {}).get("metrics") or {}
            if isinstance(metrics.get("credits"), (int, float)) and not isinstance(metrics.get("credits"), bool):
                credits = int(metrics["credits"])
        if "minArtifacts" in approve:
            put("approve_min_artifacts", artifacts >= int(approve["minArtifacts"]))
        if "minCredits" in approve:
            put("approve_min_credits", credits is not None and credits >= int(approve["minCredits"]))
    if "duplicateIdempotent" in expect:
        resumes = [turn for turn in turns if turn.get("action") == "resume" and turn.get("approved") is True]
        if len(resumes) >= 2:
            first = _artifact_count(_last_message(resumes[0].get("events") or []))
            second = _artifact_count(_last_message(resumes[1].get("events") or []))
            put("duplicate_idempotent", resumes[1].get("httpStatus") == 200 and first >= 1 and first == second)
        else:
            put("duplicate_idempotent", False)
    if "conflictStatus" in expect:
        put("confirm_conflict", int(last_turn.get("httpStatus")) if isinstance(last_turn.get("httpStatus"), int) else -1)
    follow = expect.get("afterContinue") or {}
    if follow:
        draft_turn, continue_turn = _continue_pair(observation)
        draft = (draft_turn or {}).get("measured") if draft_turn else {}
        measured = (continue_turn or {}).get("measured") if continue_turn else {}
        draft = draft if isinstance(draft, dict) else {}
        measured = measured if isinstance(measured, dict) else {}
        continue_events = [event for event in ((continue_turn or {}).get("events") or []) if isinstance(event, dict)]
        draft_ids = draft.get("draftStepIds") or []
        interrupt_ids = measured.get("interruptStepIds") or []
        if "hasPlan" in follow:
            # Every direct creation now has a plan. A new unrelated single-step
            # plan must not masquerade as restoring the cancelled DAG.
            displayed = (any(event.get("type") == "todo_plan" for event in continue_events)
                or any(_plan_part_count(event.get("message")) == 1 for event in continue_events if event.get("kind") == "message"))
            put("continue_has_plan", displayed and bool(draft_ids) and draft_ids == interrupt_ids)
        if "cursorKept" in follow:
            put(
                "continue_cursor_kept",
                isinstance(draft.get("draftIndex"), int)
                and draft.get("draftIndex") == measured.get("interruptIndex")
                and draft_ids == interrupt_ids
                and bool(draft_ids),
            )
        draft_status = dict(zip(draft_ids, draft.get("draftStatuses") or []))
        interrupt_status = dict(zip(interrupt_ids, measured.get("interruptStatuses") or []))
        if "completedKept" in follow:
            kept = all(interrupt_status.get(ident) == "completed" for ident, status in draft_status.items() if status == "completed")
            put("continue_completed_kept", kept and bool(interrupt_ids))
        if "notRedone" in follow:
            before = {ident for ident, status in draft_status.items() if status == "completed"}
            after = {ident for ident, status in interrupt_status.items() if status == "completed"}
            put("continue_not_redone", before == after)
        if "minCompletedBeforeStop" in follow:
            completed = sum(1 for status in (draft.get("draftStatuses") or []) if status == "completed")
            put("continue_min_completed", completed >= int(follow["minCompletedBeforeStop"]))
    if "draftCleared" in expect:
        measured = last_turn.get("measured") if isinstance(last_turn.get("measured"), dict) else {}
        put("draft_cleared", measured.get("draftPresent") is False)
    measured_queue = queue
    queue_expect = expect.get("queue") if isinstance(expect.get("queue"), dict) else {}
    if "rateLimit429" in queue_expect:
        put("rate_limit_429", measured_queue.get("rateLimitStatus") == 429)
    if "hasRetryAfter" in queue_expect:
        put("retry_after", bool(measured_queue.get("retryAfter")) or isinstance(measured_queue.get("retryAfterMs"), int))
    if "concurrencyRejected" in queue_expect:
        put("concurrency_cap", measured_queue.get("concurrencyStatus") == 429)
    if "singleExecution" in queue_expect:
        ids = measured_queue.get("jobIds") or []
        put(
            "queue_idempotent",
            measured_queue.get("handlerRuns") == 1 and isinstance(ids, list) and len(ids) == 2 and ids[0] == ids[1],
        )
    if "cancelStatus" in queue_expect:
        put("queue_cancel", str(measured_queue.get("cancelStatus") or ""))
    if "timeoutStatus" in queue_expect:
        put("queue_timeout", str(measured_queue.get("timeoutStatus") or ""))
    if "backpressure" in queue_expect:
        put("queue_backpressure", measured_queue.get("backpressureMessage") == QUEUE_FULL)
    if "expired" in queue_expect:
        put("queue_expiry", measured_queue.get("expired") is True and measured_queue.get("reborn") is True)
    if "bullmqRefused" in queue_expect:
        put(
            "bullmq_refused",
            measured_queue.get("bullmqMessage") == DRIVER_ERROR and measured_queue.get("redisMessage") == DRIVER_ERROR,
        )
    return answers, trace_paths


def failure_rows(state: Any, cases: list) -> list[dict]:
    """失败定位:layer、caseId、reason、Trace 路径。"""
    by_id = {case.caseId: case for case in cases}
    grades_by_trial: dict[str, list] = {}
    for grade in state.grades:
        grades_by_trial.setdefault(grade.trialId, []).append(grade)
    rows = []
    for trial in state.trials:
        case = by_id.get(trial.caseId)
        if case is None or case.taskType != "agent_flow":
            continue
        layer = trial.flowLayer or case_layer(list(case.suites))
        if trial.status == "completed" and trial.qualityVerdict == "pass":
            continue
        reasons = [grade.checkId for grade in grades_by_trial.get(trial.trialId, []) if grade.status in {"fail", "error", "inconclusive"}]
        if not reasons:
            reasons = [trial.errorCategory or trial.status or "execution_failed"]
        for reason in reasons:
            rows.append(
                {
                    "layer": layer,
                    "suite": layer,
                    "caseId": trial.caseId,
                    "reason": reason,
                    "tracePath": trial.tracePath,
                    "tracePaths": list(trial.tracePaths),
                }
            )
    return rows
