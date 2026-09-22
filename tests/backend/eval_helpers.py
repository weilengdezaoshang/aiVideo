"""评测测试共用的小构造器;保持无状态,供多个测试模块导入。"""

import json


def make_case(case_id="t2i-demo-01", prompt="一只白猫", **overrides):
    case = {
        "schemaVersion": 2,
        "caseId": case_id,
        "version": 1,
        "language": "zh-CN",
        "taskType": "text_to_image",
        "input": {"prompt": prompt, "referenceArtifactIds": [], "params": {"seed": 7}},
        "checks": [
            {"id": "has_cat", "kind": "boolean", "question": "图中是否出现猫?", "expected": True}
        ],
        "expectedOutcome": {"execution": "completed", "quality": "all_required_pass"},
        "provenance": {"source": "internal"},
    }
    case.update(overrides)
    return case


def write_jsonl(path, cases):
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n", encoding="utf-8"
    )
    return path
