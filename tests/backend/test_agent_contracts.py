"""Wire contracts: every fixture case must parse (and round-trip) or be rejected."""
import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError
import pytest

from backend.agent import contracts

FIXTURES = json.loads((Path(__file__).parent.parent / "fixtures" / "agent-contracts" / "cases.json")
                      .read_text(encoding="utf-8"))
MODELS = {name: TypeAdapter(getattr(contracts, name)) for name in
          {case["model"] for case in FIXTURES}}


@pytest.mark.parametrize("case", FIXTURES, ids=[case["name"] for case in FIXTURES])
def test_fixture_case(case):
    adapter = MODELS[case["model"]]
    if case["valid"]:
        assert adapter.dump_python(adapter.validate_python(case["value"]), mode="json") == case["value"]
    else:
        with pytest.raises(ValidationError):
            adapter.validate_python(case["value"])


def test_fixtures_cover_every_contract_family_with_positive_and_negative_cases():
    for model in ("Envelope", "ControlReceipt", "Plan", "Finish", "Reflection", "PersonaProfile",
                  "ArtifactEvidence", "CheckEvidence", "ApprovalEvidence", "ValidationReport"):
        verdicts = {case["valid"] for case in FIXTURES if case["model"] == model}
        assert verdicts == {True, False}, model
    statuses = {case["value"].get("status") for case in FIXTURES
                if case["model"] == "Envelope" and case["valid"]}
    assert statuses == {"ok", "error", "duplicate"}


def test_reserved_evidence_kinds_have_no_tool_schema_surface():
    """Reserved generation contracts are schema-only: no tool exposes them this round."""
    from backend.agent.tools import GATEWAY

    surface = json.dumps(GATEWAY.schemas(), ensure_ascii=False)
    for reserved in ("ArtifactEvidence", "ApprovalEvidence", "CheckEvidence", "ValidationReport",
                     "approval", "preserve_subject"):
        assert reserved not in surface
    assert not [name for name, spec in GATEWAY.specs.items() if spec.kind == "effect"]
