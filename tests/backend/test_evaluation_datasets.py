"""数据集版本治理验收(技术方案 §21 DATA-01/DATA-02)。"""

import json

import pytest

from backend.evaluation.datasets import DatasetStore, load_cases, validate_source_params
from backend.evaluation.models import CaseVersion
from backend.evaluation.runner import EvaluationRunner

from eval_helpers import make_case, write_jsonl


def test_data01_frozen_snapshot_unchanged_after_source_edit(tmp_path):
    source = write_jsonl(tmp_path / "ds.jsonl", [make_case()])
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")
    v1 = runner.freeze_dataset(source, "ds-zh")

    runner.create_run("run-a", "ds-zh")
    snapshot_before = DatasetStore.load_run_snapshot(runner.run_dir("run-a"))

    # run 创建后修改源用例:改提示词与期望值
    edited = make_case(prompt="两只黑狗", checks=[
        {"id": "has_dog", "kind": "boolean", "question": "图中是否出现狗?", "expected": True}
    ])
    write_jsonl(source, [edited])

    # 旧 run 的快照与规则保持不变
    snapshot_after = DatasetStore.load_run_snapshot(runner.run_dir("run-a"))
    assert snapshot_after[0].model_dump() == snapshot_before[0].model_dump()
    assert snapshot_after[0].input.prompt == "一只白猫"
    assert snapshot_after[0].contentHash == snapshot_before[0].contentHash

    # 修改后的源文件冻结产生新版本,不覆盖旧版本;manifest 记录源文件哈希变化
    v2 = runner.freeze_dataset(source, "ds-zh")
    assert v2["version"] == v1["version"] + 1
    assert v2["contentHash"] != v1["contentHash"]
    assert v1["sourceContentHash"] != v2["sourceContentHash"]

    # 同内容重复冻结幂等
    v2_again = runner.freeze_dataset(source, "ds-zh")
    assert v2_again == v2


def test_data01_run_manifest_freezes_plan_and_case_hash(tmp_path):
    source = write_jsonl(tmp_path / "ds.jsonl", [make_case()])
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")
    runner.freeze_dataset(source, "ds-zh")
    manifest = runner.create_run("run-b", "ds-zh")
    source.write_text(json.dumps(make_case(prompt="已修改"), ensure_ascii=False), encoding="utf-8")
    reread = runner.load_run("run-b").manifest
    assert reread.model_dump() == manifest.model_dump()
    assert reread.plan[0].caseContentHash == manifest.plan[0].caseContentHash


def test_data02_publish_validation_rejects_bad_datasets(tmp_path):
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")

    duplicate = write_jsonl(tmp_path / "dup.jsonl", [make_case(), make_case()])
    with pytest.raises(ValueError, match="caseId 重复"):
        runner.freeze_dataset(duplicate, "dup-ds")

    cyclic = make_case(checks=[
        {"id": "chk-a", "kind": "boolean", "question": "a?", "expected": True, "dependsOn": ["chk-b"]},
        {"id": "chk-b", "kind": "boolean", "question": "b?", "expected": True, "dependsOn": ["chk-a"]},
    ])
    with pytest.raises(ValueError, match="依赖成环"):
        CaseVersion.model_validate(cyclic)

    unknown_dep = make_case(checks=[
        {"id": "chk-a", "kind": "boolean", "question": "a?", "expected": True, "dependsOn": ["ghost"]}
    ])
    with pytest.raises(ValueError, match="依赖不存在"):
        CaseVersion.model_validate(unknown_dep)

    bad_weight = make_case(checks=[
        {"id": "chk-a", "kind": "boolean", "question": "a?", "expected": True, "weight": 0}
    ])
    with pytest.raises(ValueError):
        CaseVersion.model_validate(bad_weight)

    bad_expected = make_case(checks=[
        {"id": "chk-a", "kind": "integer", "question": "几个?", "expected": "三"}
    ])
    with pytest.raises(ValueError, match="integer"):
        CaseVersion.model_validate(bad_expected)

    duplicate_checks = make_case(checks=[
        {"id": "chk-a", "kind": "boolean", "question": "a?", "expected": True},
        {"id": "chk-a", "kind": "boolean", "question": "重复ID?", "expected": True},
    ])
    with pytest.raises(ValueError, match="检查项 ID 重复"):
        CaseVersion.model_validate(duplicate_checks)

    inconsistent = write_jsonl(tmp_path / "inconsistent.jsonl", [
        make_case("t2i-demo-video", **{"taskType": "text_to_video", "input": {
            "prompt": "测试", "params": {"kind": "image", "seed": 1},
        }})
    ])
    problems = validate_source_params(load_cases(inconsistent))
    assert problems and "不一致" in problems[0]
    with pytest.raises(ValueError, match="预检失败"):
        runner.freeze_dataset(inconsistent, "video-ds")

    # 编辑类用例必须携带引用素材;素材存在性在 run 创建时按对象库校验(§7 适配)
    edit_case = write_jsonl(tmp_path / "edit.jsonl", [
        make_case("t2i-demo-edit", **{"taskType": "image_edit"})
    ])
    problems = validate_source_params(load_cases(edit_case))
    assert problems and "需要引用素材" in problems[0]
    assert any("蒙版" in problem for problem in problems)


def test_freeze_is_immutable(tmp_path):
    """同版本目录不可覆盖:直接篡改冻结内容后重新冻结必须报错而非静默改写。"""
    source = write_jsonl(tmp_path / "ds.jsonl", [make_case()])
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")
    manifest = runner.freeze_dataset(source, "imm-ds")
    version_dir = tmp_path / "store" / "manifests" / "imm-ds" / str(manifest["version"])
    cases_file = version_dir / "cases.jsonl"
    original = cases_file.read_text(encoding="utf-8")
    cases_file.write_text(original.replace("一只白猫", "篡改内容"), encoding="utf-8")
    with pytest.raises(ValueError, match="哈希不一致"):
        DatasetStore(tmp_path / "store").load("imm-ds", manifest["version"])
