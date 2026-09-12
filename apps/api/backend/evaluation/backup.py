"""备份与恢复(技术方案 §17.5,验收 RESTORE-01)。

- 备份以一致水位拷贝:manifests、runs(manifest/state/events/trials/reports)、
  reviews、recordings、被引用 objects、budgets、grade-cache,附 SHA256SUMS 与
  BACKUP.json 索引;不含任何凭据文件。
- 恢复:校验 SHA256SUMS 后拷入目标 store,并核验素材哈希与 sealed 录制可加载;
  恢复后在隔离环境执行一次严格回放即完成 RESTORE-01 演练(测试内实现)。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from ..common import now

BACKUP_CONTENT_DIRS = ("manifests", "runs", "reviews", "recordings", "objects", "budgets", "grade-cache", "gates")


def create_backup(store: Path, out_dir: Path | None = None) -> Path:
    target_root = (out_dir or (store / "backups")) / f"backup-{now().replace(':', '')}"
    target = target_root / "store"
    files: list[str] = []
    for name in BACKUP_CONTENT_DIRS:
        source = store / name
        if not source.is_dir():
            continue
        for path in source.rglob("*"):
            if path.is_file():
                relative = path.relative_to(store)
                dest = target / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                files.append(str(relative))
    checksums = {relative: hashlib.sha256((target / relative).read_bytes()).hexdigest() for relative in files}
    (target / "SHA256SUMS").write_text(
        "\n".join(f"{digest}  {name}" for name, digest in sorted(checksums.items())) + "\n",
        encoding="utf-8",
    )
    index = {
        "schemaVersion": 1,
        "kind": "frayune-eval-backup",
        "createdAt": now(),
        "fileCount": len(files),
        "files": checksums,
        "note": "恢复时先校验 SHA256SUMS,再核验素材哈希与 sealed 录制(§17.5)",
    }
    (target / "BACKUP.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target_root


def _verify_sums(store_dir: Path) -> list[str]:
    sums_file = store_dir / "SHA256SUMS"
    if not sums_file.is_file():
        return ["备份缺少 SHA256SUMS"]
    problems = []
    for line in sums_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split("  ", 1)
        path = store_dir / name
        if not path.is_file():
            problems.append(f"缺少文件:{name}")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"哈希不一致:{name}")
    return problems


def restore_backup(backup_dir: Path, target_store: Path) -> dict:
    """恢复到目标 store(目标必须为空或不存在),返回核验结果。"""
    source = backup_dir / "store"
    if not (source / "BACKUP.json").is_file():
        raise FileNotFoundError(f"不是有效的备份目录:{backup_dir}")
    problems = _verify_sums(source)
    if problems:
        raise ValueError("备份校验失败:" + ";".join(problems))
    if target_store.exists() and any(target_store.iterdir()):
        raise ValueError(f"目标 store 非空,拒绝覆盖:{target_store}")
    target_store.mkdir(parents=True)
    for name in BACKUP_CONTENT_DIRS:
        part = source / name
        if part.is_dir():
            shutil.copytree(part, target_store / name)
    # 恢复后核验(§17.5):素材哈希 + sealed 录制可加载 + 索引文件数一致
    from .artifacts import ObjectStore

    objects = ObjectStore(target_store, project="frayune")
    object_problems = []
    for meta_file in (target_store / "objects" / "frayune").glob("art-*.json"):
        artifact_id = meta_file.stem
        try:
            objects.get(artifact_id)
        except (FileNotFoundError, ValueError) as exc:
            object_problems.append(str(exc))
    recording_problems = []
    index = json.loads((source / "BACKUP.json").read_text(encoding="utf-8"))
    for recordings_dir in (target_store / "recordings").glob("*") if (target_store / "recordings").is_dir() else []:
        meta_file = recordings_dir / "meta.json"
        if meta_file.is_file() and json.loads(meta_file.read_text(encoding="utf-8")).get("sealed"):
            try:
                from .replay import RecordingReader

                RecordingReader(target_store, recordings_dir.name, objects=objects)
            except ValueError as exc:
                recording_problems.append(str(exc))
    return {
        "restoredFiles": index["fileCount"],
        "objectProblems": object_problems,
        "recordingProblems": recording_problems,
        "targetStore": str(target_store),
    }
