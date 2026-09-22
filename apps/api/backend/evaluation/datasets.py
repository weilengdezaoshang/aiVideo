"""用例、数据集与版本治理(技术方案 §4):v2 JSONL 校验、冻结快照、加载。

冻结语义(§4.4/§6.2):同 datasetId 同版本目录一经写入不可变更;同内容重复冻结
幂等返回原版本;同版本不同内容直接报错。run 创建时另行复制一份快照进 run 目录,
即使 manifests 被清理,旧 run 仍可独立判分与重渲染(DATA-01)。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ..common import now
from .models import CaseVersion, DatasetManifest, canonical_hash

SUPPORTED_INPUT_PARAMS = {
    "kind",
    "width",
    "height",
    "steps",
    "cfgScale",
    "seed",
    "batchCount",
    "sampler",
    "scheduler",
    "denoise",
    "durationSec",
    "fps",
    "negativePrompt",
    "model",
}


@dataclass
class FrozenDataset:
    manifest: DatasetManifest
    cases: list[CaseVersion]


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"数据集文件不存在:{path}")
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{path.name} 第 {number} 行不是合法 JSON:{exc}") from None
        if not isinstance(item, dict):
            raise ValueError(f"{path.name} 第 {number} 行必须是 JSON 对象")
        entries.append(item)
    if not entries:
        raise ValueError(f"{path.name} 没有任何用例")
    return entries


def parse_cases(entries: list[dict]) -> list[CaseVersion]:
    cases: list[CaseVersion] = []
    seen: set[str] = set()
    for index, item in enumerate(entries, 1):
        try:
            case = CaseVersion.model_validate(item)
        except ValidationError as exc:
            raise ValueError(f"第 {index} 条用例校验失败:{exc.errors()[0]['msg']}") from None
        if case.caseId in seen:
            raise ValueError(f"caseId 重复:{case.caseId}(§4.4 发布校验)")
        seen.add(case.caseId)
        cases.append(case)
    return cases


def validate_source_params(cases: list[CaseVersion]) -> list[str]:
    """发布前检查(§4.4):结构层校验;素材存在性与 Provider 能力匹配在 run 创建时
    按对象库与能力快照判断(§1.2),这里只校验任务类型与参数的一致性。"""
    problems: list[str] = []
    for case in cases:
        unknown = set(case.input.params) - SUPPORTED_INPUT_PARAMS
        if unknown:
            problems.append(f"{case.caseId}:不支持的生成参数 {sorted(unknown)}")
        kind = case.input.params.get("kind", "image")
        if case.taskType in {"text_to_video", "image_to_video"} and kind != "video":
            problems.append(f"{case.caseId}:taskType=video 但 params.kind={kind} 不一致")
        if case.taskType in {"text_to_image", "image_to_image", "image_edit"} and kind == "video":
            problems.append(f"{case.caseId}:taskType=image 但 params.kind=video 不一致")
        if case.taskType in {"image_to_image", "image_edit", "image_to_video"} and not case.input.referenceArtifactIds:
            problems.append(f"{case.caseId}:{case.taskType} 需要引用素材(归档 artifact)")
        if case.taskType == "image_edit" and len(case.input.referenceArtifactIds) < 2:
            problems.append(f"{case.caseId}:image_edit 需要原图 + PNG 蒙版两个素材")
        if case.taskType == "text_to_image" and case.input.referenceArtifactIds:
            problems.append(f"{case.caseId}:text_to_image 不应携带参考素材")
    return problems


def load_cases(path: Path) -> list[CaseVersion]:
    return parse_cases(read_jsonl(path))


class DatasetStore:
    def __init__(self, root: Path):
        self.root = root / "manifests"

    def _version_dir(self, dataset_id: str, version: int) -> Path:
        return self.root / dataset_id / str(version)

    def list_versions(self, dataset_id: str) -> list[DatasetManifest]:
        base = self.root / dataset_id
        if not base.is_dir():
            return []
        manifests = []
        for directory in sorted(base.iterdir(), key=lambda p: p.name):
            manifest_file = directory / "manifest.json"
            if manifest_file.is_file():
                manifests.append(DatasetManifest.model_validate(json.loads(manifest_file.read_text(encoding="utf-8"))))
        return manifests

    def freeze(
        self,
        cases: list[CaseVersion],
        dataset_id: str,
        split: str = "dev",
        source_path: Path | None = None,
    ) -> DatasetManifest:
        case_hashes = [case.contentHash for case in cases]
        content_hash = canonical_hash(case_hashes)
        for manifest in self.list_versions(dataset_id):
            if manifest.contentHash == content_hash:
                return manifest  # 幂等:同内容已是冻结版本
        existing = {m.version for m in self.list_versions(dataset_id)}
        version = (max(existing) + 1) if existing else 1
        manifest = DatasetManifest(
            datasetId=dataset_id,
            version=version,
            split=split,
            frozenAt=now(),
            caseCount=len(cases),
            contentHash=content_hash,
            caseHashes=case_hashes,
            sourcePath=str(source_path) if source_path else None,
            sourceContentHash=(
                hashlib.sha256(source_path.read_bytes()).hexdigest() if source_path else None
            ),
        )
        target = self._version_dir(dataset_id, version)
        if target.exists():
            raise ValueError(f"数据集版本目录已存在,拒绝覆盖:{target}")
        target.mkdir(parents=True)
        (target / "manifest.json").write_text(
            json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (target / "cases.jsonl").write_text(
            "\n".join(case.model_dump_json() for case in cases) + "\n", encoding="utf-8"
        )
        return manifest

    def load(self, dataset_id: str, version: int | None = None) -> FrozenDataset:
        if version is None:
            manifests = self.list_versions(dataset_id)
            if not manifests:
                raise FileNotFoundError(f"数据集不存在:{dataset_id}")
            manifest = manifests[-1]
            version = manifest.version
        directory = self._version_dir(dataset_id, version)
        manifest_file = directory / "manifest.json"
        if not manifest_file.is_file():
            raise FileNotFoundError(f"数据集版本不存在:{dataset_id}@{version}")
        manifest = DatasetManifest.model_validate(
            json.loads(manifest_file.read_text(encoding="utf-8"))
        )
        cases = parse_cases(read_jsonl(directory / "cases.jsonl"))
        if canonical_hash([case.contentHash for case in cases]) != manifest.contentHash:
            raise ValueError(f"数据集快照哈希不一致:{dataset_id}@{version}")
        return FrozenDataset(manifest=manifest, cases=cases)

    def snapshot_into(self, dataset_id: str, version: int, run_dir: Path) -> dict:
        """run 创建时复制快照(§5.3),返回写入 run manifest 的 dataset 引用。"""
        frozen = self.load(dataset_id, version)
        target = run_dir / "dataset"
        target.mkdir(parents=True, exist_ok=True)
        (target / "cases.jsonl").write_text(
            "\n".join(case.model_dump_json() for case in frozen.cases) + "\n", encoding="utf-8"
        )
        (target / "manifest.json").write_text(
            json.dumps(frozen.manifest.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest = frozen.manifest.model_dump()
        return {
            "datasetId": manifest["datasetId"],
            "version": manifest["version"],
            "contentHash": manifest["contentHash"],
            "frozenAt": manifest["frozenAt"],
        }

    @staticmethod
    def load_run_snapshot(run_dir: Path) -> list[CaseVersion]:
        """判分与报告只读 run 自己的快照,不回头读源用例文件(DATA-01)。"""
        snapshot = run_dir / "dataset" / "cases.jsonl"
        if not snapshot.is_file():
            raise FileNotFoundError(f"run 缺少数据集快照:{run_dir}")
        return parse_cases(read_jsonl(snapshot))
