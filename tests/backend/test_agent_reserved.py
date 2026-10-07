"""Reserved generation interfaces: effect admission parks the Run; nothing executes."""
import pytest
from pydantic import BaseModel

from backend.agent import orchestrator as orch
from backend.agent import policy as pol
from backend.agent import verifier
from backend.agent.tools import GATEWAY
from backend.agent.tools.registry import ToolSpec
from backend.agent.state import State
from backend.agent.tools import project_snapshot
from backend.agent.kernel import Limits

LIMITS = Limits(max_model_calls=20, max_tool_calls=24, tool_timeout_s=15, tool_heartbeat_s=5)


class StubInput(BaseModel):
    prompt: str


def stub_spec() -> ToolSpec:
    return ToolSpec(name="stub_generate", version=1, description="stub effect tool", input=StubInput,
                    kind="effect", stages=frozenset({"execute"}), timeout_s=5, handler=None,
                    facts=lambda outcome, args: [], suggest=lambda outcome, args: [])


def state(stage="execute"):
    objects = {"o1": {"id": "o1", "kind": "image", "x": 0, "y": 0, "width": 10, "height": 10}}
    return State(stage=stage, snapshot=project_snapshot(
        {"id": "d", "name": "n", "revision": 7, "order": list(objects), "objects": objects}, []),
        message="生成一张图", counters={}, rows=(), tool_call_count=0)


def test_only_the_answer_suite_is_registered_and_unknown_recipes_are_refused():
    assert verifier.registered_suites() == ("answer",)
    assert verifier.suite_for("answer").run is verifier.verify_finish
    with pytest.raises(ValueError):
        verifier.suite_for("preserve_subject_replace_background")
    with pytest.raises(ValueError):
        verifier.register(verifier.CheckSuite("answer", verifier.verify_finish))


def test_admit_effect_always_waits_for_approval():
    admission = pol.Policy(GATEWAY).admit_effect("stub_generate", {"prompt": "田园风格"})
    assert isinstance(admission, pol.WaitApproval)
    assert admission.tool == "stub_generate"
    assert admission.key == pol.Policy(GATEWAY).key("stub_generate", {"prompt": "田园风格"})


def test_reducer_parks_the_run_waiting_approval_and_executes_nothing(monkeypatch):
    monkeypatch.setitem(GATEWAY.specs, "stub_generate", stub_spec())
    monkeypatch.setattr(orch, "stage_tools", lambda stage, step_tools=(): ["stub_generate"])
    out = orch.on_model(state(), [{"id": "c1", "name": "stub_generate",
                                   "arguments": {"prompt": "田园风格"}, "raw_arguments": "{}"}],
                        "tool_calls", None, 1, LIMITS)
    assert out.status == "waiting_approval"
    assert out.run["stage"] == "waiting"
    assert out.run["waiting"] == {"tool": "stub_generate", "arguments": {"prompt": "田园风格"},
                                  "stage": "execute"}
    assert "approval.requested" in out.events
    effect_row, receipt = out.plans[0], out.plans[1]
    assert (effect_row.kind, effect_row.status, effect_row.output) == ("effect", "waiting_approval", None)
    assert receipt.output["effect"] == {"waiting": "approval", "tool": "stub_generate"}
    # The stub handler is None and was never invoked: no envelope, no approval evidence.
    assert effect_row.output is None


def test_production_registry_has_no_effect_tools_at_startup():
    assert not [name for name, spec in GATEWAY.specs.items() if spec.kind == "effect"]
