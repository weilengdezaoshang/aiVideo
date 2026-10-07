"""Cross-Run memory. Episodic memory carries earlier answers as *claims about a past version*."""
from __future__ import annotations

import re

_CITATION = re.compile(r"\s*\[E\d+(?:\.f\d+)?\]")


QUESTION_PREFIX = "（上一轮我向你提出了一个问题，等待你的回答）"


def strip_citations(text: str) -> str:
    """Evidence ids are only valid inside the Run that produced them."""
    return _CITATION.sub("", text)


def stale_note(past_revision: int, current_revision: int) -> str:
    return f"（基于画布版本 {past_revision} 的旧回答，当前版本为 {current_revision}，需重新核实）"


def episodic(previous: list, current_revision: int) -> tuple[list[dict], list[str]]:
    """Messages for completed Runs, oldest first, plus the ids of the Runs they came from.

    ``previous`` are completed Runs with a verified reply. Answers about another canvas
    version are marked so they cannot be mistaken for facts about the current one.
    """
    messages: list[dict] = []
    for prior in previous:
        if prior.status == "waiting_input":
            answer = QUESTION_PREFIX + strip_citations(prior.waiting["question"])
        else:
            answer = strip_citations(prior.reply)
        past = prior.snapshot.get("revision")
        if past != current_revision:
            answer = stale_note(past, current_revision) + answer
        messages.extend([{"role": "user", "content": prior.message}, {"role": "assistant", "content": answer}])
    return messages, [str(prior.id) for prior in previous]
