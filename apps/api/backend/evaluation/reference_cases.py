"""Labeled Chinese session-reference cases. No model calls."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.core.session_references import ArtifactCandidate

FIXTURE_PATH = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "session-reference-cases.jsonl"
Category = Literal["time", "ordinal", "spatial", "selection", "ambiguous", "unavailable", "isolation"]


class ReferenceCase(BaseModel):
    """Caller session and batch stay outside the utterance. The parser reads only `utterance`."""

    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    utterance: str = Field(min_length=1)
    category: Category
    caller_session_id: str = Field(min_length=1)
    batch_id: str | None = None
    artifacts: list[ArtifactCandidate]
    expected_status: Literal["resolved", "ambiguous", "unavailable"]
    expected_artifact_id: str | None = None
    expected_reason: str = Field(min_length=1)


def _art(artifact_id, *, session="s1", ordinal=1, batch=None, index=None, permitted=True,
         available=True, selected=False, x=None, y=None, asset=None) -> dict:
    return {
        "artifact_id": artifact_id, "session_id": session, "ordinal": ordinal,
        "batch_id": batch, "batch_index": index, "asset_id": asset or f"asset-{artifact_id}",
        "permitted": permitted, "available": available, "selected": selected, "x": x, "y": y,
    }


def _case(case_id, utterance, category, kind, artifacts, status, reason, artifact=None, **query) -> dict:
    del kind
    return {
        "case_id": case_id, "utterance": utterance, "category": category,
        "caller_session_id": "s1", "batch_id": query.get("batch_id"),
        "artifacts": artifacts, "expected_status": status,
        "expected_artifact_id": artifact, "expected_reason": reason,
    }


def case_rows() -> list[dict]:
    a = _art
    rows = [
        _case("ref-time-latest", "刚才那个再暗一点", "time", "latest",
              [a("p1", ordinal=1, batch="b", index=1), a("p2", ordinal=2, batch="b", index=2)],
              "resolved", "latest", "p2"),
        _case("ref-time-only", "刚刚那张", "time", "latest", [a("only")], "resolved", "latest", "only"),
        _case("ref-time-previous-round", "上一张图", "time", "latest",
              [a("old", ordinal=1), a("new", ordinal=4)], "resolved", "latest", "new"),
        _case("ref-time-empty", "刚才那个", "time", "latest", [], "unavailable", "no_artifact"),
        _case("ref-time-unavailable", "刚才那个产物", "unavailable", "latest",
              [a("dead", ordinal=2, available=False), a("older", ordinal=1)],
              "unavailable", "asset_unavailable", "dead"),
        _case("ref-time-hidden-latest", "刚才那个", "isolation", "latest",
              [a("secret", ordinal=3, permitted=False), a("visible", ordinal=2)],
              "resolved", "latest", "visible"),
        _case("ref-ord-second", "第二张", "ordinal", "ordinal",
              [a("a1", batch="b", index=1), a("a2", batch="b", index=2), a("a3", batch="b", index=3)],
              "resolved", "ordinal", "a2", ordinal=2, batch_id="b"),
        _case("ref-ord-first", "第一张", "ordinal", "ordinal",
              [a("a1", batch="b", index=1), a("a2", batch="b", index=2)],
              "resolved", "ordinal", "a1", ordinal=1, batch_id="b"),
        _case("ref-ord-third", "第三张", "ordinal", "ordinal",
              [a("a1", batch="b", index=1), a("a2", batch="b", index=2), a("a3", batch="b", index=3)],
              "resolved", "ordinal", "a3", ordinal=3, batch_id="b"),
        _case("ref-ord-out", "第四张", "ordinal", "ordinal",
              [a("a1", batch="b", index=1), a("a2", batch="b", index=2)],
              "unavailable", "ordinal_out_of_range", ordinal=4, batch_id="b"),
        _case("ref-ord-no-batch", "第二张", "ambiguous", "ordinal",
              [a("a1", batch="b1", index=2), a("c1", batch="b2", index=2)],
              "ambiguous", "ordinal_batch_unspecified", ordinal=2),
        _case("ref-ord-single-batch", "第二张", "ordinal", "ordinal",
              [a("a1", batch="only", index=1), a("a2", batch="only", index=2)],
              "resolved", "ordinal", "a2", ordinal=2),
        _case("ref-ord-dead-second", "第二张坏了不要换别的", "unavailable", "ordinal",
              [a("a1", batch="b", index=1), a("a2", batch="b", index=2, available=False), a("a3", batch="b", index=3)],
              "unavailable", "asset_unavailable", "a2", ordinal=2, batch_id="b"),
        _case("ref-ord-other-session", "第二张", "isolation", "ordinal",
              [a("foreign", session="s2", batch="b", index=2), a("mine", batch="b", index=1)],
              "unavailable", "ordinal_out_of_range", ordinal=2, batch_id="b"),
        _case("ref-ord-unpermitted", "第二张", "isolation", "ordinal",
              [a("hidden", batch="b", index=2, permitted=False), a("first", batch="b", index=1)],
              "unavailable", "ordinal_out_of_range", ordinal=2, batch_id="b"),
        _case("ref-ord-tie", "第二张有两份", "ambiguous", "ordinal",
              [a("x", batch="b", index=2, ordinal=1), a("y", batch="b", index=2, ordinal=2)],
              "ambiguous", "ordinal_tie", ordinal=2, batch_id="b"),
        _case("ref-left", "左边那个", "spatial", "spatial",
              [a("l", x=10, y=0), a("r", x=80, y=0)], "resolved", "spatial", "l", spatial="left"),
        _case("ref-right", "右边那个", "spatial", "spatial",
              [a("l", x=10, y=5), a("r", x=90, y=5)], "resolved", "spatial", "r", spatial="right"),
        _case("ref-left-three", "最左边那张", "spatial", "spatial",
              [a("m", x=40), a("l", x=5), a("r", x=70)], "resolved", "spatial", "l", spatial="left"),
        _case("ref-right-three", "最右边那张", "spatial", "spatial",
              [a("m", x=40), a("l", x=5), a("r", x=70)], "resolved", "spatial", "r", spatial="right"),
        _case("ref-spatial-tie", "左边那个分不清", "ambiguous", "spatial",
              [a("a", x=10), a("b", x=10.2), a("c", x=40)],
              "ambiguous", "spatial_tie", spatial="left"),
        _case("ref-spatial-missing", "左边那个", "unavailable", "spatial",
              [a("nogeo"), a("also")], "unavailable", "missing_geometry", spatial="left"),
        _case("ref-spatial-skip-dead", "左边那个还在的", "spatial", "spatial",
              [a("gone", x=1, available=False), a("live", x=20)],
              "resolved", "spatial", "live", spatial="left"),
        _case("ref-spatial-other-session", "左边那个", "isolation", "spatial",
              [a("foreign", session="s2", x=0), a("mine", x=30)],
              "resolved", "spatial", "mine", spatial="left"),
        _case("ref-select-one", "就用我选中的", "selection", "selected",
              [a("s", selected=True), a("o")], "resolved", "selected", "s"),
        _case("ref-select-beats-latest", "刚才那个", "selection", "latest",
              [a("old", ordinal=1, selected=True), a("new", ordinal=9)],
              "resolved", "selected", "old"),
        _case("ref-select-beats-left", "左边那个", "selection", "spatial",
              [a("left", x=0), a("picked", x=50, selected=True)],
              "resolved", "selected", "picked", spatial="right"),
        _case("ref-select-beats-ordinal", "第二张", "selection", "ordinal",
              [a("first", batch="b", index=1, selected=True), a("second", batch="b", index=2)],
              "resolved", "selected", "first", ordinal=2, batch_id="b"),
        _case("ref-select-two", "选中的两张", "ambiguous", "selected",
              [a("a", selected=True), a("b", selected=True)], "ambiguous", "multiple_selected"),
        _case("ref-select-none", "用选中的", "unavailable", "selected",
              [a("a"), a("b")], "unavailable", "no_selection"),
        _case("ref-select-dead", "选中的那张失效了", "unavailable", "selected",
              [a("dead", selected=True, available=False)], "unavailable", "asset_unavailable", "dead"),
        _case("ref-select-other-session", "选中的", "isolation", "selected",
              [a("foreign", session="s2", selected=True), a("local")],
              "unavailable", "no_selection"),
        _case("ref-select-unpermitted", "选中的", "isolation", "selected",
              [a("hidden", selected=True, permitted=False)], "unavailable", "not_permitted"),
        _case("ref-cross-only", "刚才那个", "isolation", "latest",
              [a("foreign", session="other", ordinal=1)], "unavailable", "cross_session_isolated"),
        _case("ref-ambiguous-batches-left-ok", "第二张还是另一批", "ambiguous", "ordinal",
              [a("a", batch="p", index=1), a("b", batch="q", index=1)],
              "ambiguous", "ordinal_batch_unspecified", ordinal=1),
        _case("ref-empty-session-with-foreign", "右边那个", "isolation", "spatial",
              [a("foreign", session="s9", x=1)], "unavailable", "cross_session_isolated", spatial="right"),
        _case("ref-ord-batch-missing", "这一批的第二张并不存在", "unavailable", "ordinal",
              [a("a", batch="other", index=2)], "unavailable", "no_artifact", ordinal=2, batch_id="missing"),
        _case("ref-time-three", "刚才那张再看一次", "time", "latest",
              [a("a", ordinal=1), a("b", ordinal=2), a("c", ordinal=3)],
              "resolved", "latest", "c"),
        _case("ref-right-epsilon-clear", "右边那个明显分开", "spatial", "spatial",
              [a("a", x=0), a("b", x=3)], "resolved", "spatial", "b", spatial="right"),
        _case("ref-select-and-unavailable-sibling", "用选中的，别的失效不管", "selection", "latest",
              [a("keep", ordinal=1, selected=True), a("gone", ordinal=5, available=False)],
              "resolved", "selected", "keep"),
        _case("ref-intent-both-sides", "左边那个和右边那个", "ambiguous", "spatial",
              [a("l", x=0), a("r", x=40)], "ambiguous", "ambiguous_reference_intent"),
        _case("ref-intent-ordinal-and-side", "第一张和左边那个", "ambiguous", "ordinal",
              [a("a1", batch="b", index=1, x=0), a("a2", batch="b", index=2, x=30)],
              "ambiguous", "ambiguous_reference_intent", batch_id="b"),
        _case("ref-intent-no-reference", "生成一只兔子", "ambiguous", "latest",
              [a("only")], "unavailable", "no_reference_intent"),
    ]
    return rows


def load_cases(path: Path | None = None) -> list[ReferenceCase]:
    source = path or FIXTURE_PATH
    cases = []
    seen = set()
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = ReferenceCase.model_validate_json(line)
        if case.case_id in seen:
            raise ValueError(f"duplicate reference case {case.case_id}")
        seen.add(case.case_id)
        cases.append(case)
    if len(cases) < 36:
        raise ValueError("session reference cases must contain at least 36 rows")
    return cases


def write_fixture(path: Path | None = None) -> Path:
    target = path or FIXTURE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in case_rows()]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_cases(target)
    return target
