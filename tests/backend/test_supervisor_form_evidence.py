"""Canvas JSON and unrelated history never become authoritative form answers."""
from types import SimpleNamespace
import uuid

from backend.core.supervisor.reviewer import merge_proposed


def test_canvas_and_unowned_message_fields_cannot_authorize_duration():
    run = SimpleNamespace(id=uuid.uuid4(), invariants={}, snapshot={
        "request": {"message": "生成视频"}, "form_answers": {"duration_seconds": 5}})
    proposed = {"deliverable": {"duration_seconds": 5}}
    for owner in (None, str(uuid.uuid4())):
        messages = [{"run_id": owner, "role": "user", "content": {
            "task_id": "pretended-task", "fields": {"duration_seconds": 5}}}]
        merged, reasons = merge_proposed(run, proposed, messages)
        assert merged == {} and reasons[0]["code"] == "invariant_unproven"
    messages = [{"run_id": str(run.id), "role": "user", "content": {
        "task_id": "owned-task", "fields": {"duration_seconds": 5}}}]
    merged, reasons = merge_proposed(run, proposed, messages)
    assert not reasons and merged["deliverable"]["duration_seconds"] == 5


def test_option_label_is_not_a_count_or_duration_grant():
    run = SimpleNamespace(id="run-1", invariants={}, snapshot={"request": {"message": "生成视频"}})
    messages = [{"role": "user", "run_id": "run-1", "content": {"task_id": "choice", "text": "8秒，3张"}}]
    for proposed in ({"deliverable": {"duration_seconds": 8}}, {"deliverable": {"count": 3}}):
        merged, reasons = merge_proposed(run, proposed, messages)
        assert merged == {} and reasons[0]["code"] == "invariant_unproven"


def test_control_quote_accepts_only_current_run_answer_fields():
    import pytest
    from backend.core.supervisor.tool_calls import _evidence, ConversionError
    args = {"source_quote": "8", "source": "user_form", "invariants": {"deliverable": {"duration_seconds": 8}}}
    context = {"run_id": "run-1", "request": {"message": "生成视频"}, "snapshot": {"form_answers": {"duration_seconds": 8}},
               "messages": [{"role": "user", "run_id": "run-1", "content": {"task_id": "answer", "fields": {"duration_seconds": 8}}}]}
    assert _evidence(args, "生成视频", context)[0].source == "user_form"
    for owner in (None, "other-run"):
        context["messages"][0]["run_id"] = owner
        with pytest.raises(ConversionError, match="invariant_evidence_required"):
            _evidence(args, "生成视频", context)
