"""故障开关必须使真实流程规则变红，并在同一进程还原为绿。"""
import os
import json
from pathlib import Path

import pytest

from scripts.verify_agent_eval_sensitivity import FAULTS, verify


@pytest.mark.parametrize("fault", list(FAULTS))
def test_fault_red_then_restored_green(tmp_path: Path, monkeypatch, fault: str):
    monkeypatch.delenv("AGENT_EVAL_FAULT", raising=False)
    rows = verify(tmp_path, fault)
    assert [row["verdict"] for row in rows] == ["PASS", "FAIL", "PASS"]
    red = rows[1]
    assert red["suite"] == FAULTS[fault][0]
    assert red["caseId"] == FAULTS[fault][1]
    assert red["reason"] == FAULTS[fault][3]
    assert all(Path(row["tracePath"]).is_file() for row in rows)
    assert rows[0]["reason"] is None and rows[2]["reason"] is None
    assert red["checkId"] == FAULTS[fault][2]
    assert "AGENT_EVAL_FAULT" not in os.environ


def test_full_acceptance_has_five_steps_and_all_layers(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_EVAL_FAULT", raising=False)
    verify(tmp_path)
    steps = json.loads((tmp_path / "acceptance.json").read_text())
    assert [step["verdict"] for step in steps] == ["PASS", "FAIL", "FAIL", "FAIL", "FAIL", "PASS"]
    for step in (steps[0], steps[-1]):
        assert step["caseCount"] == 30
        assert {case["suite"] for case in step["cases"]} == {f"L{i}" for i in range(6)} | {"L7"}
        assert all(Path(case["observationPath"]).is_file() for case in step["cases"])
    assert "AGENT_EVAL_FAULT" not in os.environ


def test_existing_evidence_is_preserved(tmp_path, monkeypatch):
    evidence = tmp_path / "runs" / "existing" / "manifest.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("do not overwrite")
    monkeypatch.setenv("AGENT_EVAL_FAULT", "f2")
    with pytest.raises(ValueError, match="证据目录已有运行"):
        verify(tmp_path)
    assert evidence.read_text() == "do not overwrite"
    assert os.environ["AGENT_EVAL_FAULT"] == "f2"
