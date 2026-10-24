from types import SimpleNamespace

from backend.core.batch.runtime import successor_task_keys


def test_failed_branch_does_not_cancel_an_independent_sibling_in_the_same_item():
    document = {"steps": [
        {"id": "t1", "depends_on": []}, {"id": "t2", "depends_on": ["t1"]},
        {"id": "t3", "depends_on": []}, {"id": "t4", "depends_on": ["t2", "t3"]},
    ], "items": [{"steps": [{"step_id": key, "task_key": key} for key in ("t1", "t2", "t3", "t4")]}]}
    tasks = {key: SimpleNamespace(status="validated", error=None) for key in ("t1", "t2", "t3", "t4")}
    tasks["t1"].status = "failed"
    assert set(successor_task_keys(document, tasks)) == {"t2", "t4"}
    tasks["t4"].status = "submitted"
    assert successor_task_keys(document, tasks) == ["t2"]


def test_uncertain_submitted_timeout_is_not_a_failure_or_a_license_to_resubmit():
    document = {"steps": [{"id": "t1"}, {"id": "t2", "depends_on": ["t1"]}],
                "items": [{"steps": [{"step_id": key, "task_key": key} for key in ("t1", "t2")]}]}
    tasks = {"t1": SimpleNamespace(status="submitted", error={"code": "timeout"}),
             "t2": SimpleNamespace(status="validated", error=None)}
    assert successor_task_keys(document, tasks) == []
