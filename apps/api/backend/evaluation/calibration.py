"""裁判校准:人工独立标注集导入、版本保留与校准报告(技术方案 §11.3)。

- 校准样本为 check 级配对:裁判自动判定(auto_defect)与人工独立标注(human_defect)。
- 任一侧 None(未定/证据不足)不计入混淆矩阵分母,单独计数,不强行充当确定标签。
- 校准记录绑定裁判模型 + rubric 版本 + 数据划分;更换任一项 = 新校准。
- 未通过校准(或样本不足)时,质量门禁按 inconclusive 处理,不默认通过。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

from ..common import atomic_write_text, now
from .registry import judge_calibration_report


class CalibrationSample(BaseModel):
    caseId: str
    checkId: str
    autoDefect: bool | None = None  # 裁判判定:check 级 fail = 缺陷
    humanDefect: bool | None = None  # 人工独立标注
    category: str = "全部"
    split: str = "calibration_test"  # train/dev/test 划分保留,防泄漏


class CalibrationRecord(BaseModel):
    schemaVersion: int = 1
    calibrationId: str
    judgeModel: str
    rubricVersion: str = "1"
    promptVersion: str = "1"
    labelerVersion: str = "1"  # 标注规范版本
    datasetRef: str = ""  # 标注集来源说明(文件/数据集引用)
    sampleCount: int = 0
    splits: dict[str, int] = Field(default_factory=dict)
    report: dict = Field(default_factory=dict)
    createdAt: str = Field(default_factory=now)


class CalibrationStore:
    def __init__(self, store: Path):
        self.root = store / "calibrations"

    def _path(self, calibration_id: str) -> Path:
        return self.root / f"{calibration_id}.json"

    def import_samples(
        self,
        samples: list[dict],
        judge_model: str,
        rubric_version: str = "1",
        prompt_version: str = "1",
        labeler_version: str = "1",
        dataset_ref: str = "",
    ) -> CalibrationRecord:
        if not judge_model.strip():
            raise ValueError("校准必须绑定裁判模型")
        if not samples:
            raise ValueError("校准样本为空")
        parsed = []
        problems = []
        for index, item in enumerate(samples, 1):
            try:
                parsed.append(CalibrationSample.model_validate(item))
            except Exception as exc:  # noqa: BLE001 - 逐条报错便于修正标注文件
                problems.append(f"第 {index} 条:{exc}")
        if problems:
            raise ValueError("校准样本校验失败:" + ";".join(problems[:5]))
        report = judge_calibration_report(
            [
                {
                    "auto_defect": sample.autoDefect,
                    "human_defect": sample.humanDefect,
                    "category": sample.category,
                }
                for sample in parsed
            ]
        )
        splits: dict[str, int] = {}
        for sample in parsed:
            splits[sample.split] = splits.get(sample.split, 0) + 1
        record = CalibrationRecord(
            calibrationId=f"cal-{uuid.uuid4().hex[:12]}",
            judgeModel=judge_model,
            rubricVersion=rubric_version,
            promptVersion=prompt_version,
            labelerVersion=labeler_version,
            datasetRef=dataset_ref,
            sampleCount=len(parsed),
            splits=splits,
            report=report,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path(record.calibrationId),
            json.dumps(
                {**record.model_dump(), "samples": [s.model_dump() for s in parsed]},
                ensure_ascii=False,
                indent=2,
            ),
        )
        return record

    def list_for_judge(self, judge_model: str, rubric_version: str) -> list[dict]:
        if not self.root.is_dir():
            return []
        records = []
        for path in sorted(self.root.glob("cal-*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("judgeModel") == judge_model and data.get("rubricVersion") == rubric_version:
                records.append(data)
        return records

    def list_all(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob("cal-*.json"))
        ]
