"""Deterministic hard checks (H1-H6) of a submitted answer against the Run's evidence.

Pure functions over stored tool rows. A model can never overrule these results: an
answer that fails any check cannot become a completed Run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable, Iterable

from backend.agent.contracts import Finish

INLINE_REF = re.compile(r"\[(E\d+(?:\.f\d+)?)\]")
ANY_REF = re.compile(r"\[?E\d+(?:\.f\d+)?\]?")
REF_FORMAT = re.compile(r"^E(\d+)(?:\.f(\d+))?$")
_FULLWIDTH = str.maketrans("０１２３４５６７８９．，", "0123456789.,")
CHECKS = ("H1", "H2", "H3", "H4", "H5", "H6")
TOLERANCE = 1e-9


@dataclass(frozen=True)
class EvidenceEntry:
    id: str
    status: str                     # ok | error
    revision: int
    complete: bool
    facts: dict[str, dict]
    numbers: tuple[float, ...]
    open_required: tuple[dict, ...]


@dataclass(frozen=True)
class Issue:
    check: str
    code: str
    detail: str
    claim: int | None = None
    ref: str | None = None

    def public(self) -> dict:
        return {"check": self.check, "code": self.code, "detail": self.detail,
                "claim": self.claim, "ref": self.ref}


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues

    @property
    def fatal(self) -> bool:
        return any(issue.check == "H5" for issue in self.issues)

    def checks(self) -> list[dict]:
        return [{"id": name, "passed": not any(i.check == name for i in self.issues),
                 "issues": [i.public() for i in self.issues if i.check == name]} for name in CHECKS]

    def public(self) -> dict:
        return {"passed": self.passed, "checks": self.checks()}


def numbers_in(text: str) -> list[float]:
    """Quantities in a claim. Inline refs, ordinals (第 N) and thousands commas are not quantities."""
    cleaned = ANY_REF.sub(" ", text.translate(_FULLWIDTH))
    cleaned = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", cleaned)
    cleaned = re.sub(r"第\s*\d+(?:\.\d+)?", " ", cleaned)
    return [float(token) for token in re.findall(r"\d+(?:\.\d+)?", cleaned)]


def _leaf_numbers(value: Any) -> Iterable[float]:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield float(value)
    elif isinstance(value, str):
        yield from numbers_in(value)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _leaf_numbers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _leaf_numbers(child)


def build_index(rows: list[dict], key_of: Callable[[str, dict], str]) -> dict[str, EvidenceEntry]:
    """Map evidence ids to entries. ``rows`` are read-tool rows ordered by ordinal."""
    executed = {key_of(row["name"], row["arguments"]) for row in rows
                if isinstance(row.get("output"), dict) and row["output"].get("status") == "ok"}
    index: dict[str, EvidenceEntry] = {}
    for row in rows:
        envelope = row.get("output")
        if row.get("kind") != "read" or not isinstance(envelope, dict) or envelope.get("kind") != "tool":
            continue
        ident, status = envelope["evidenceId"], envelope["status"]
        if status == "duplicate":
            if envelope["dedupeOf"] in index:
                index[ident] = index[envelope["dedupeOf"]]
            continue
        revision = envelope["source"]["revision"]
        if status == "error":
            index[ident] = EvidenceEntry(ident, "error", revision, False, {}, (), ())
            continue
        result = envelope["result"]
        open_required = tuple(item for item in result["next"] if item["priority"] == "required"
            and not (item["action"] == "call_tool" and item["arguments"] is not None
                     and key_of(item["tool"], item["arguments"]) in executed))
        numbers = tuple(_leaf_numbers({"data": result["data"], "page": result["page"]}))
        index[ident] = EvidenceEntry(ident, "ok", revision, result["coverage"]["complete"],
                                     {fact["id"]: fact for fact in result["evidence"]}, numbers, open_required)
    return index


def _resolve(index: dict[str, EvidenceEntry], ref: str) -> tuple[EvidenceEntry, dict | None] | None:
    match = REF_FORMAT.match(ref)
    if not match:
        return None
    entry = index.get(f"E{match.group(1)}")
    if entry is None:
        return None
    if match.group(2) is None:
        return entry, None
    for fact_id, fact in entry.facts.items():
        # An aliased duplicate keeps the original's fact ids; refs use the original id.
        if fact_id == ref:
            return entry, fact
    return None


def _envelope_ref(ref: str) -> str:
    return ref.split(".")[0]


def verify_finish(finish: Finish, index: dict[str, EvidenceEntry], revision: int) -> Report:
    report = Report()
    inline = list(dict.fromkeys(INLINE_REF.findall(finish.answer)))
    claim_envelopes = {_envelope_ref(ref) for claim in finish.claims for ref in claim.evidenceRefs}
    for ref in inline:
        if _resolve(index, ref) is None:
            report.issues.append(Issue("H1", "UNKNOWN_REF", f"回答引用的 {ref} 不存在于本次运行的证据中", ref=ref))
        elif _envelope_ref(ref) not in claim_envelopes:
            report.issues.append(Issue("H1", "INLINE_REF_WITHOUT_CLAIM", f"回答引用 {ref}，但没有对应的 claim", ref=ref))
    for position, claim in enumerate(finish.claims):
        refs = list(dict.fromkeys(claim.evidenceRefs))
        resolved = []
        if not refs and claim.scope != "unavailable":
            report.issues.append(Issue("H1", "CLAIM_WITHOUT_EVIDENCE", "断言没有引用任何证据", claim=position))
        for ref in refs:
            found = _resolve(index, ref)
            if found is None:
                report.issues.append(Issue("H1", "UNKNOWN_REF", f"断言引用的 {ref} 不存在于本次运行的证据中",
                                           claim=position, ref=ref))
            else:
                resolved.append((ref, *found))
        for ref, entry, fact in resolved:
            if entry.revision != revision:
                report.issues.append(Issue("H5", "STALE_EVIDENCE",
                    f"{ref} 来自画布版本 {entry.revision}，运行冻结版本为 {revision}", claim=position, ref=ref))
            if entry.status == "error" and claim.scope != "unavailable":
                report.issues.append(Issue("H2", "ERROR_EVIDENCE_CITED",
                    f"{ref} 是失败的工具结果，只能用于 scope=unavailable 的断言", claim=position, ref=ref))
            if claim.scope == "complete" and entry.status == "ok":
                if fact is not None and fact["scope"] != "complete":
                    report.issues.append(Issue("H3", "NOT_COMPLETE_FACT",
                        f"{ref} 只证明{ '部分内容' if fact['scope'] == 'partial' else '单个对象' }，不能支撑完整性断言",
                        claim=position, ref=ref))
                elif fact is None and (not entry.complete or entry.open_required):
                    report.issues.append(Issue("H3", "INCOMPLETE_EVIDENCE",
                        f"{ref} 是部分结果或仍有必需的后续读取，请改用 partial 表述或先完成读取",
                        claim=position, ref=ref))
        allowed: list[float] = []
        for _ref, entry, fact in resolved:
            if entry.status != "ok":
                continue
            allowed.extend(_leaf_numbers(fact["value"]) if fact is not None else
                           [*entry.numbers, *(n for f in entry.facts.values() for n in _leaf_numbers(f["value"]))])
        for number in numbers_in(claim.text):
            if not any(abs(number - candidate) < TOLERANCE for candidate in allowed):
                shown = int(number) if number.is_integer() else number
                report.issues.append(Issue("H4", "NUMBER_NOT_IN_EVIDENCE",
                    f"断言中的数字 {shown} 没有出现在所引证据中", claim=position))
    if index and not finish.claims:
        report.issues.append(Issue("H6", "NO_CLAIMS", "运行中调用过工具，回答必须至少给出一条带证据的断言"))
    return report


def required_next_actions(index: dict[str, EvidenceEntry], finish: Finish) -> list[dict]:
    """Open `required` suggestions of the evidence an answer claimed to be complete."""
    seen, actions = set(), []
    for claim in finish.claims:
        for ref in claim.evidenceRefs:
            entry = index.get(_envelope_ref(ref))
            for item in (entry.open_required if entry else ()):
                marker = repr(item)
                if marker not in seen:
                    seen.add(marker)
                    actions.append(item)
    return actions
