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
    for model in ("Envelope", "ControlReceipt", "Plan", "Finish", "Reflection", "PersonaProfile"):
        verdicts = {case["valid"] for case in FIXTURES if case["model"] == model}
        assert verdicts == {True, False}, model
    statuses = {case["value"].get("status") for case in FIXTURES
                if case["model"] == "Envelope" and case["valid"]}
    assert statuses == {"ok", "error", "duplicate"}
