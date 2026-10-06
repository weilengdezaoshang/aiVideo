"""Control tools: schemas the model sees, resolved by the loop policy and never executed.

``finish`` ends a Run and carries the claims the verifier checks. ``read_evidence``
replays an earlier envelope of the same Run (it creates no new evidence).
"""
from __future__ import annotations

from pydantic import Field

from backend.agent.contracts import Finish
from backend.agent.tools.errors import ToolInput
from backend.agent.tools.registry import ToolSpec

CONTROL_NAMES = frozenset({"finish", "read_evidence"})


class ReadEvidenceInput(ToolInput):
    evidence_id: str = Field(alias="evidenceId", pattern=r"^E\d+$")


def _no_facts(outcome, parsed):
    return []


def _no_next(outcome, parsed):
    return []


CONTROL_TOOLS = [
    ToolSpec("finish", 1,
             "提交最终回答并结束运行。answer 中每个事实之后用 [E编号.f编号] 标注证据；claims 逐条给出断言文本、"
             "evidenceRefs 与 scope（complete=完整结论，partial=部分，item=单个对象，unavailable=无法获得）。"
             "断言中的数字必须来自所引证据；证据为部分结果时不得使用 complete。必须单独调用。",
             Finish, "control", frozenset({"respond", "legacy"}), 5, None, _no_facts, _no_next),
    ToolSpec("read_evidence", 1, "按证据编号（如 E3）重新读取本次运行中某次工具调用的完整结果。",
             ReadEvidenceInput, "control", frozenset({"plan", "execute", "legacy"}), 5, None, _no_facts, _no_next),
]
