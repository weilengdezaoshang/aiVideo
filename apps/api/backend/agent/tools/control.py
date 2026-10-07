"""Control tools: schemas the model sees, resolved by the loop policy and never executed.

``finish`` ends a Run and carries the claims the verifier checks. ``read_evidence``
replays an earlier envelope of the same Run (it creates no new evidence).
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from backend.agent.contracts import Finish, Plan, Reflection
from backend.agent.tools.errors import ToolInput
from backend.agent.tools.registry import ToolSpec

CONTROL_NAMES = frozenset({"submit_plan", "complete_step", "report_blocker", "ask_user", "finish",
                           "submit_reflection", "read_evidence"})


class ReadEvidenceInput(ToolInput):
    evidence_id: str = Field(alias="evidenceId", pattern=r"^E\d+$")


class CompleteStepInput(ToolInput):
    summary: str = Field(min_length=1, max_length=300)
    evidence_refs: list[str] = Field(alias="evidenceRefs", max_length=8)
    partial: bool = False


class ReportBlockerInput(ToolInput):
    code: Literal["tool_error", "insufficient_data", "out_of_scope", "other"]
    detail: str = Field(min_length=1, max_length=300)


class AskUserInput(ToolInput):
    question: str = Field(min_length=1, max_length=300)


def _no_facts(outcome, parsed):
    return []


def _no_next(outcome, parsed):
    return []


def _control(name, description, model, stages):
    return ToolSpec(name, 1, description, model, "control", frozenset(stages), 5, None, _no_facts, _no_next)


CONTROL_TOOLS = [
    _control("submit_plan", "提交执行计划：目标 goal 和 1–6 个步骤。每步写明 key（小写字母开头）、title、intent、"
             "要使用的只读工具 tools、依赖 dependsOn 与可检验的完成标准 doneWhen。必须单独调用。", Plan, {"plan"}),
    _control("complete_step", "完成当前步骤：summary 写结论，evidenceRefs 列出本步骤内取得的证据（如 E2 或 E2.f1）。"
             "若仍有必需的后续读取却不继续，须设置 partial=true 说明结论只是部分的。必须单独调用。",
             CompleteStepInput, {"execute"}),
    _control("report_blocker", "当前步骤无法完成（工具错误、数据不足或超出能力）时报告原因，运行时会据此重新规划。"
             "必须单独调用。", ReportBlockerInput, {"execute"}),
    _control("ask_user", "需求不清、需要用户补充信息时向用户提问；运行进入等待状态。必须单独调用。",
             AskUserInput, {"plan", "execute"}),
    _control("finish",
             "提交最终回答。answer 中每个事实之后用 [E编号.f编号] 标注证据；claims 逐条给出断言文本、"
             "evidenceRefs 与 scope（complete=完整结论，partial=部分，item=单个对象，unavailable=无法获得）。"
             "断言中的数字必须来自所引证据；证据为部分结果时不得使用 complete。必须单独调用。", Finish, {"respond"}),
    _control("submit_reflection",
             "提交复盘结论：accept（可以交付）、revise_answer（只需改写回答）、retry_step（某步骤需重做，issue 中写 stepKey）、"
             "replan（计划有问题）、ask_user（需要用户补充，写 question）。每个问题必须给出 evidenceRefs 或 stepKey；"
             "accept 时 issues 必须为空。必须单独调用。", Reflection, {"reflect"}),
    _control("read_evidence", "按证据编号（如 E3）重新读取本次运行中某次工具调用的完整结果。",
             ReadEvidenceInput, {"plan", "execute", "respond", "reflect"}),
]
