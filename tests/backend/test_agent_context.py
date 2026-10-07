"""Context assembly under a byte budget: required layers are kept, optional ones trimmed in order."""
import json
from types import SimpleNamespace

import pytest

from backend.agent import memory
from backend.agent.cognition.roles import ANALYST
from backend.agent.context import ContextLimit, Group, build_context, compact_content, size_of
from backend.agent.persona import DEFAULT_PERSONA
from backend.agent.util import encoded


def envelope(ordinal, payload="x" * 400):
    return {"evidenceId": f"E{ordinal}", "kind": "tool", "status": "ok", "tool": "read_canvas",
            "result": {"data": {"blob": payload}, "evidence": [{"id": f"E{ordinal}.f1", "statement": "事实"}],
                       "coverage": {"complete": True}, "next": [{"reason": "继续", "priority": "suggested"}]}}


def turn(ordinal, payload="x" * 400):
    call = {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{ordinal}"}]}
    tool = {"role": "tool", "tool_call_id": f"c{ordinal}", "content": encoded(envelope(ordinal, payload))}
    return Group([call, tool], (f"E{ordinal}",))


def pair(index):
    return [{"role": "user", "content": f"问题{index}" + "问" * 300},
            {"role": "assistant", "content": f"回答{index}" + "答" * 300}]


def build(budget, episodic=(), groups=(), intent="你好"):
    messages = [m for index in range(len(episodic)) for m in pair(index)] if isinstance(episodic, range) else list(episodic)
    return build_context(persona=DEFAULT_PERSONA, persona_scope="full", role=ANALYST, scene={"revision": 1},
        episodic=messages, memory_sources=[f"run{i}" for i in range(len(messages) // 2)], intent=intent,
        groups=list(groups), budget_bytes=budget, toolset_version="t@1")


def test_everything_fits_without_trimming_and_the_manifest_matches_the_messages():
    built = build(100000, range(2), [turn(1), turn(2)])
    assert [m["role"] for m in built.messages][:6] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert built.manifest["trimmed"] == [] and built.manifest["totalBytes"] == size_of(built.messages)
    assert built.manifest["evidenceIds"] == ["E1", "E2"] and built.manifest["memorySources"] == ["run0", "run1"]
    layer = {item["name"]: item for item in built.manifest["layers"]}
    assert not any(item["trimmed"] for item in layer.values())
    assert layer["episodic"]["bytes"] > 0 and layer["observation"]["bytes"] > 0


def test_episodic_memory_goes_first_oldest_pair_first():
    full = build(100000, range(4))
    budget = full.manifest["totalBytes"] - 100
    built = build(budget, range(4))
    assert [t["layer"] for t in built.manifest["trimmed"]] == ["episodic"]
    assert built.messages[1]["content"].startswith("问题1") and built.manifest["memorySources"] == ["run1", "run2", "run3"]
    assert built.manifest["totalBytes"] <= budget
    assert {i["name"]: i for i in built.manifest["layers"]}["episodic"]["trimmed"] is True


def test_older_observations_are_compacted_before_the_newest_is_touched():
    groups = [turn(1, "a" * 3000), turn(2, "b" * 3000), turn(3, "c" * 3000)]
    full = build(10**6, groups=groups)
    budget = full.manifest["totalBytes"] - 4000
    built = build(budget, groups=groups)
    compacted = [t for t in built.manifest["trimmed"] if t["layer"] == "observation"]
    assert [t["evidenceIds"] for t in compacted] == [["E1"], ["E2"]] or [t["evidenceIds"] for t in compacted] == [["E1"]]
    tool_messages = [m for m in built.messages if m["role"] == "tool"]
    newest = json.loads(tool_messages[-1]["content"])
    assert newest["result"]["data"] == {"blob": "c" * 3000}
    oldest = json.loads(tool_messages[0]["content"])
    assert oldest["result"]["data"] == {"compacted": True} and oldest["result"]["evidence"] and oldest["result"]["next"]
    assert built.manifest["totalBytes"] <= budget


def test_episodic_is_trimmed_before_observations():
    groups = [turn(1, "a" * 3000), turn(2, "b" * 200)]
    built = build(size_of(build(10**6, range(2), groups).messages) - 200, range(2), groups)
    assert [t["layer"] for t in built.manifest["trimmed"]] == ["episodic"]


def test_required_layers_never_trim_and_overflow_raises_without_dropping_protection():
    with pytest.raises(ContextLimit):
        build(4096, intent="长" * 3000)
    with pytest.raises(ContextLimit):
        build(10, range(3), [turn(1)])
    base = build(10**6)
    exact = base.manifest["totalBytes"]
    assert build(exact).messages == base.messages
    with pytest.raises(ContextLimit):
        build(exact - 1)


def test_compaction_only_touches_envelopes_and_replayed_evidence():
    assert compact_content("not json") == "not json"
    assert compact_content(json.dumps({"kind": "tool", "status": "error", "error": {"code": "X"}})).count("X") == 1
    receipt = {"kind": "control", "tool": "read_evidence", "status": "accepted", "effect": {"envelope": envelope(1)}}
    assert json.loads(compact_content(json.dumps(receipt)))["effect"] == {"compacted": True}
    rejected = {"kind": "control", "tool": "finish", "status": "rejected", "effect": None, "error": {"code": "CLAIMS_INVALID"}}
    assert json.loads(compact_content(json.dumps(rejected)))["error"]["code"] == "CLAIMS_INVALID"


def prior(index, reply, revision):
    return SimpleNamespace(id=f"r{index}", message=f"问题{index}", reply=reply, snapshot={"revision": revision},
                           status="completed", waiting=None)


def test_episodic_memory_strips_citations_and_marks_other_versions():
    messages, sources = memory.episodic([prior(1, "共 63 张[E2.f1]，首页 50 张 [E2]。", 5), prior(2, "没有变化。", 7)], 7)
    assert sources == ["r1", "r2"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1]["content"] == memory.stale_note(5, 7) + "共 63 张，首页 50 张。"
    assert "[E" not in messages[1]["content"]
    assert messages[3]["content"] == "没有变化。" and messages[0]["content"] == "问题1"


def test_strip_citations_leaves_other_brackets_alone():
    assert memory.strip_citations("见[附录] 与 [E2.f1][E3]") == "见[附录] 与"
    assert memory.strip_citations("没有引用") == "没有引用"


def test_a_run_that_asked_the_user_is_remembered_as_a_pending_question():
    asked = SimpleNamespace(id="r9", message="看看图", reply=None, snapshot={"revision": 7},
                            status="waiting_input", waiting={"question": "你指的是哪一张[E1]？"})
    messages, sources = memory.episodic([asked], 7)
    assert sources == ["r9"] and messages[1]["content"] == memory.QUESTION_PREFIX + "你指的是哪一张？"
