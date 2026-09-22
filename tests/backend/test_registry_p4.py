"""P4 验收:评估器注册契约、能力预检、裁判校准、压缩实验(§1.2/§10.6/§11.3/§13.5)。"""

import pytest

from backend.evaluation.datasets import validate_source_params
from backend.evaluation.models import CaseVersion
from backend.evaluation.registry import (
    CapabilityUnsupported,
    EvaluatorDeclaration,
    all_evaluators,
    capabilities_for,
    check_case_supported,
    compress_prompt,
    get,
    judge_calibration_report,
    register,
    register_builtin,
)
from backend.evaluation.runner import EvaluationRunner

from eval_helpers import make_case, write_jsonl


# ---------- 评估器注册(§10.6) ----------


def test_evaluator_registry_contract():
    register_builtin()
    rules = get("typed-rules", "1")
    assert "text_to_image" in rules.applies_to
    assert rules.known_limits  # 必须声明已知限制
    # 未登记的评估器拒绝进入流程
    with pytest.raises(ValueError, match="未登记"):
        get("vlm-judge", "9")
    # 同版本重复登记拒绝;更换实现必须更换版本
    with pytest.raises(ValueError, match="更换实现必须更换版本"):
        register(
            EvaluatorDeclaration(
                id="typed-rules", version="1", rubric_version="1",
                applies_to={"text_to_image"}, inputs={}, results={},
            )
        )
    assert {item.id for item in all_evaluators()} >= {"typed-rules", "stub-judge", "vlm-judge"}


# ---------- 能力预检(§1.2) ----------


def test_capability_unsupported_is_rejected_not_degraded():
    caps = capabilities_for({"provider": "cloud"})
    assert caps.video is False  # cloud 快照未配置 videoModel
    video_case = CaseVersion.model_validate(
        make_case("t2i-video-01", **{"taskType": "text_to_video", "input": {
            "prompt": "一只猫走过屏幕",
            "params": {"kind": "video", "seed": 3},
        }})
    )
    with pytest.raises(CapabilityUnsupported, match="CAPABILITY_UNSUPPORTED"):
        check_case_supported(video_case, caps)
    # mock 能力快照支持视频
    mock_caps = capabilities_for({"provider": "mock"})
    check_case_supported(video_case, mock_caps)  # 不抛出


def test_runner_precheck_rejects_unsupported_video_for_live_cloud(tmp_path):
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")
    source = write_jsonl(
        tmp_path / "ds.jsonl",
        [make_case("t2i-video-01", **{"taskType": "text_to_video", "input": {
            "prompt": "一只猫走过屏幕", "params": {"kind": "video", "seed": 3},
        }})],
    )
    runner.freeze_dataset(source, "video-zh")
    with pytest.raises(ValueError, match="CAPABILITY_UNSUPPORTED"):
        runner.create_run(
            "run-video-live", "video-zh", mode="live", provider="cloud",
            budget={"maxCost": 5.0, "prices": {"generation": 0.5}},
            sandbox_config={"cloudVendor": "zhipu", "cloudBaseUrl": "http://stub.local/v1", "cloudModel": "m"},
        )


def test_mock_video_case_runs_end_to_end(tmp_path):
    """mock 视频链路(P4 开放):预检通过 → 生成 → 类型化判分。"""
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")
    source = write_jsonl(
        tmp_path / "ds.jsonl",
        [make_case("t2i-video-01", **{"taskType": "text_to_video", "input": {
            "prompt": "一只猫走过屏幕", "params": {"kind": "video", "seed": 3},
        }})],
    )
    runner.freeze_dataset(source, "video-zh")
    runner.create_run("run-video", "video-zh")
    state = runner.execute_run("run-video")
    assert state.status == "completed"
    assert state.trials[0].status == "completed"
    assert state.trials[0].artifactIds
    record, data = runner.objects.get(state.trials[0].artifactIds[0])
    assert record.mime == "image/webp"  # mock 视频为动画 WebP 夹具(显式合成)
    assert data[:4] == b"RIFF"


def test_inconsistent_task_type_and_kind_rejected(tmp_path):
    case = make_case("t2i-video-02", **{"taskType": "text_to_video", "input": {
        "prompt": "测试", "params": {"kind": "image", "seed": 1},
    }})
    problems = validate_source_params([CaseVersion.model_validate(case)])
    assert problems and "不一致" in problems[0]


# ---------- 裁判校准报告(§11.3) ----------


def test_judge_calibration_confusion_matrix():
    samples = [
        {"auto_defect": True, "human_defect": True},     # TP
        {"auto_defect": True, "human_defect": True},     # TP
        {"auto_defect": False, "human_defect": True},    # FN(漏报)
        {"auto_defect": True, "human_defect": False},    # FP(误报)
        {"auto_defect": False, "human_defect": False},   # TN
        {"auto_defect": None, "human_defect": True},     # 裁判未定:不入分母
        {"auto_defect": True, "human_defect": None},     # 人工未定:不入分母
    ]
    report = judge_calibration_report(samples, defect_category="count")
    assert report["confusion"] == {"TP": 2, "FN": 1, "FP": 1, "TN": 1}
    assert report["defectRecall"] == round(2 / 3, 4)
    assert report["falsePositiveRate"] == 0.5
    assert report["excluded"] == {"autoUnknown": 1, "humanUnknown": 1}
    # 全部分母为 0:N/A 而非假 0
    empty = judge_calibration_report([], defect_category="style")
    assert empty["defectRecall"] is None and empty["falsePositiveRate"] is None


# ---------- 压缩实验(§13.5) ----------


def test_compression_preserves_hard_constraints():
    prompt = "三只白猫,,  并排坐在  纯灰色背景前。没有其他动物!!"
    result = compress_prompt(prompt)
    assert result["compressorVersion"] == "whitespace-v1"
    assert result["compressed"] == "三只白猫,并排坐在 纯灰色背景前。没有其他动物!"
    # 硬约束必须保留:数量、否定、待渲染实体不被压缩破坏(§13.5)
    for token in ("三只", "没有", "白猫"):
        assert token in result["compressed"]
    assert result["compressedChars"] <= result["originalChars"]
    assert result["tokenDeltaEstimate"] is None  # 未知保持 None,不冒充估算
