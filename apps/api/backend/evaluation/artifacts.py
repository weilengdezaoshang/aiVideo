"""内容寻址素材库(技术方案 §6.1 objects/):sha256 即身份,项目内去重。

写入顺序遵循 §6.2:先写临时文件,计算哈希后原子 rename;引用保护与保留策略
在 P5 清理任务中实现,本模块只提供不可变写入与校验读取。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from ..common import now
from .models import ArtifactRecord

MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "mp4": "video/mp4",
    "webm": "video/webm",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ObjectStore:
    def __init__(self, root: Path, project: str = "default"):
        self.root = root / "objects" / project
        self.project = project

    def _blob(self, digest: str) -> Path:
        return self.root / digest

    def _meta(self, artifact_id: str) -> Path:
        return self.root / f"{artifact_id}.json"

    def put(self, data: bytes, ext: str, kind="image", origin_run_id=None) -> ArtifactRecord:
        if not data:
            raise ValueError("素材内容为空")
        digest = sha256_bytes(data)
        artifact_id = f"art-{digest[:16]}"
        blob, meta = self._blob(digest), self._meta(artifact_id)
        if not blob.is_file():
            self.root.mkdir(parents=True, exist_ok=True)
            temp = self.root / f".{digest}.{digest[:8]}.tmp"
            temp.write_bytes(data)
            temp.replace(blob)
        record = ArtifactRecord(
            artifactId=artifact_id,
            sha256=digest,
            mime=MIME_BY_EXT.get(ext.lower(), "application/octet-stream"),
            size=len(data),
            kind=kind,
            createdAt=now(),
            originRunId=origin_run_id,
        )
        if not meta.is_file():
            meta.write_text(
                json.dumps(record.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return record

    def get(self, artifact_id: str) -> tuple[ArtifactRecord, bytes]:
        record = self.record(artifact_id)
        blob = self._blob(record.sha256)
        if not blob.is_file():
            raise FileNotFoundError(f"素材内容缺失:{artifact_id}")
        data = blob.read_bytes()
        if sha256_bytes(data) != record.sha256:
            raise ValueError(f"素材哈希不一致,内容已损坏:{artifact_id}")
        return record, data

    def record(self, artifact_id: str) -> ArtifactRecord:
        meta = self._meta(artifact_id)
        if not meta.is_file():
            raise FileNotFoundError(f"素材不存在:{artifact_id}")
        try:
            return ArtifactRecord.model_validate(json.loads(meta.read_text(encoding="utf-8")))
        except ValidationError as exc:
            raise ValueError(f"素材元数据损坏:{artifact_id}") from exc

    def verify_all(self, artifact_ids: list[str]) -> list[str]:
        """校验全部素材可读且哈希一致,返回问题清单(REPLAY-07 预检基础)。"""
        problems = []
        for artifact_id in artifact_ids:
            try:
                self.get(artifact_id)
            except (FileNotFoundError, ValueError) as exc:
                problems.append(str(exc))
        return problems
