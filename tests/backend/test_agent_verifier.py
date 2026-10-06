"""Hard answer checks H1-H6 and number extraction, on envelopes built by the real tools."""
from pathlib import Path

import pytest

from backend.agent import evidence as ev
from backend.agent.contracts import Claim, Finish
from backend.agent.policy import Policy
from backend.agent.tools import GATEWAY, execute_envelope, project_snapshot
from backend.agent.verifier import build_index, numbers_in, verify_finish

POLICY = Policy(GATEWAY)


def canvas(count=60):
    objects = {f"o{i}": {"id": f"o{i}", "kind": "image", "x": i, "y": 0, "width": 100 + i, "height": 50}
               for i in range(count)}
    return {"id": "doc", "name": "画布", "revision": 7, "order": list(objects), "objects": objects}


class Run:
    """Rows of a Run as the kernel stores them, produced by the real gateway."""
    def __init__(self, count=60):
        self.snapshot, self.rows = project_snapshot(canvas(count), []), []

    def call(self, name, args, ok_status="completed"):
        ordinal = len(self.rows) + 1
        envelope = execute_envelope(name, args, self.snapshot, {}, Path("."), max_output_bytes=16384,
                                    ordinal=ordinal, call_id=f"c{ordinal}")
        self.rows.append({"ordinal": ordinal, "name": name, "arguments": args, "kind": "read",
                          "status": "failed" if envelope["status"] == "error" else ok_status, "output": envelope})
        return envelope

    def index(self):
        return build_index(self.rows, POLICY.key)

    def check(self, answer, *claims, revision=7):
        finish = Finish(answer=answer, claims=[Claim(**claim) for claim in claims])
        return verify_finish(finish, self.index(), revision)


def claim(text, refs, scope="item"):
    return {"text": text, "evidenceRefs": refs, "scope": scope}


def failed(report):
    return {issue.check + ":" + issue.code for issue in report.issues}


@pytest.mark.parametrize("text,expected", [
    ("共有 63 张图片", [63.0]), ("尺寸为 1024×768", [1024.0, 768.0]), ("大小 12,345 字节", [12345.0]),
    ("宽度约 3.5 倍", [3.5]), ("共１２张，宽１０２４", [12.0, 1024.0]), ("第 2 张宽 512", [512.0]),
    ("引用 [E2.f1] 后共 3 个", [3.0]), ("没有数字", []),
])
def test_number_extraction(text, expected):
    assert numbers_in(text) == expected


def test_h1_references_must_exist_and_be_backed_by_a_claim():
    run = Run()
    run.call("list_canvas_elements", {"limit": 5})
    ok = run.check("共 60 张[E1.f1]", claim("共 60 张图片", ["E1.f1"], "complete"))
    assert ok.passed, ok.issues
    assert "H1:UNKNOWN_REF" in failed(run.check("共 60 张", claim("共 60 张图片", ["E9.f1"], "complete")))
    assert "H1:UNKNOWN_REF" in failed(run.check("共 60 张", claim("共 60 张图片", ["E1.f99"], "complete")))
    assert "H1:UNKNOWN_REF" in failed(run.check("共 60 张[E4]", claim("共 60 张图片", ["E1.f1"], "complete")))
    run.call("read_canvas", {})
    assert "H1:INLINE_REF_WITHOUT_CLAIM" in failed(run.check("版本 7[E2.f1]", claim("无关", ["E1.f2"], "partial")))
    assert run.check("版本 7[E2.f1]", claim("版本为 7", ["E2.f1"], "item")).passed
    assert "H1:CLAIM_WITHOUT_EVIDENCE" in failed(run.check("共 60 张", claim("共 60 张图片", [], "complete")))


def test_control_tool_receipts_are_not_citable():
    run = Run(3)
    run.call("read_canvas", {})
    report = run.check("好[E2]", claim("好", ["E2"], "item"))   # E2 would be the finish receipt
    assert "H1:UNKNOWN_REF" in failed(report)


def test_h2_error_evidence_only_supports_unavailable_claims():
    run = Run(3)
    run.call("get_canvas_elements", {"objectIds": ["ghost"]})
    assert "H2:ERROR_EVIDENCE_CITED" in failed(run.check("读取失败", claim("读取成功", ["E1"], "item")))
    assert "H2:ERROR_EVIDENCE_CITED" in failed(run.check("读取失败", claim("对象不存在", ["E1"], "partial")))
    assert run.check("读取失败", claim("无法读取该对象", ["E1"], "unavailable")).passed


def test_h3_complete_claims_need_complete_facts_and_finished_reads():
    run = Run(60)
    run.call("list_canvas_elements", {"limit": 50})
    total = run.check("共 60 张", claim("共 60 张图片", ["E1.f1"], "complete"))
    assert total.passed, total.issues                                        # total is a complete fact
    page = run.check("本页 50 个", claim("本页返回 50 个对象", ["E1.f2"], "complete"))
    assert "H3:NOT_COMPLETE_FACT" in failed(page)
    whole = run.check("共 60 张", claim("共 60 张图片", ["E1"], "complete"))
    assert "H3:INCOMPLETE_EVIDENCE" in failed(whole)                         # envelope is partial
    assert run.check("本页 50 个", claim("本页返回 50 个对象", ["E1.f2"], "partial")).passed
    run.call("list_canvas_elements", {"limit": 50, "offset": 50})            # finishes the required read
    index = run.index()
    assert not index["E1"].open_required


def test_h3_envelope_claim_is_complete_only_when_nothing_is_left_to_read():
    run = Run(5)
    run.call("list_canvas_elements", {"limit": 50})
    assert run.check("共 5 个", claim("共 5 个对象", ["E1"], "complete")).passed


def test_h4_numbers_must_equal_cited_values():
    run = Run(60)
    run.call("get_canvas_elements", {"objectIds": ["o1", "o2"]})
    ok = run.check("尺寸", claim("返回的第 1 个对象位置 (1,0)，尺寸 101×50", ["E1.f2"]))
    assert ok.passed, ok.issues
    assert "H4:NUMBER_NOT_IN_EVIDENCE" in failed(run.check("尺寸", claim("尺寸 999×50", ["E1.f2"])))
    assert "H4:NUMBER_NOT_IN_EVIDENCE" in failed(run.check("尺寸", claim("尺寸 101×50", ["E1.f3"])))   # o2 is 102 wide
    assert run.check("尺寸", claim("尺寸 102×50", ["E1.f3"])).passed
    assert run.check("数量", claim("返回 2 个对象", ["E1.f1"], "complete")).passed
    assert "H4:NUMBER_NOT_IN_EVIDENCE" in failed(run.check("数量", claim("返回 3 个对象", ["E1.f1"], "complete")))


def test_h4_envelope_level_citation_may_use_any_number_in_its_data():
    run = Run(3)
    run.call("get_canvas_elements", {"objectIds": ["o1"]})
    assert run.check("宽度", claim("宽度为 101", ["E1"])).passed
    assert not run.check("宽度", claim("宽度为 555", ["E1"])).passed


def test_h5_evidence_from_another_revision_is_fatal():
    run = Run(3)
    run.call("read_canvas", {})
    report = run.check("版本", claim("版本为 7", ["E1.f1"]), revision=8)
    assert report.fatal and "H5:STALE_EVIDENCE" in failed(report)


def test_h6_an_answer_after_tool_use_needs_claims_but_a_tool_free_answer_does_not():
    run = Run(3)
    assert Finish(answer="你好", claims=[]) and verify_finish(Finish(answer="你好", claims=[]), run.index(), 7).passed
    run.call("read_canvas", {})
    assert "H6:NO_CLAIMS" in failed(run.check("没有断言"))


def test_duplicate_calls_resolve_to_the_original_evidence():
    run = Run(5)
    run.call("read_canvas", {})
    original = run.rows[0]["output"]
    duplicate = ev.duplicate_envelope(tool="read_canvas", ordinal=2, call_id="c2", step_key=None,
        snapshot=run.snapshot, dedupe_of="E1", next_actions=[])
    run.rows.append({"ordinal": 2, "name": "read_canvas", "arguments": {}, "kind": "read", "status": "completed",
                     "output": duplicate})
    index = run.index()
    assert index["E2"] is index["E1"] and original["evidenceId"] == "E1"
    assert run.check("版本", claim("版本为 7", ["E2"])).passed


def test_report_lists_every_check_with_its_issues():
    run = Run(3)
    run.call("read_canvas", {})
    public = run.check("没有断言").public()
    assert public["passed"] is False and [c["id"] for c in public["checks"]] == ["H1", "H2", "H3", "H4", "H5", "H6"]
    assert [c["id"] for c in public["checks"] if not c["passed"]] == ["H6"]
