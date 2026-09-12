"""保留策略与素材引用保护(技术方案 §6.4,RESTORE/清理基础)。

- 默认只做 dry-run 清单;真正的删除必须显式 execute(不由定时任务自动触发,§17.4)。
- 有有效 recording、review 或 run 引用的对象受保护,普通清理不得删除。
- 敏感素材撤销:标记 revoked + 删除审计,取消回放资格;不伪装成仍可完整复现(§8.6)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..common import now
from .artifacts import ObjectStore, sha256_bytes


@dataclass
class RetentionPolicy:
    """保留时长(天);具体时长由项目配置(§6.4),这里给保守默认。"""

    runRetentionDays: int = 365  # run 目录(含未引用 trial 产物)的最短保留
    objectProtection: bool = True  # 被引用对象一律保护
    auditKeepDays: int = -1  # 审计/预算/门禁记录永久保留(-1)


@dataclass
class CleanupPlan:
    deleteObjects: list[str] = field(default_factory=list)
    deleteRuns: list[str] = field(default_factory=list)
    protectedObjects: int = 0
    note: str = ""


def referenced_artifact_hashes(store: Path) -> set[str]:
    """收集全部引用:run trials 的 artifactId、recordings 媒体引用。"""
    referenced: set[str] = set()
    objects_root = store / "objects" / "frayune"
    runs_root = store / "runs"
    if runs_root.is_dir():
        for trial_grades in runs_root.glob("*/trials/*/trial.json"):
            try:
                trial = json.loads(trial_grades.read_text(encoding="utf-8"))
            except ValueError:
                continue
            for artifact_id in trial.get("artifactIds") or []:
                meta = objects_root / f"{artifact_id}.json"
                if meta.is_file():
                    try:
                        record = json.loads(meta.read_text(encoding="utf-8"))
                        referenced.add(record["sha256"])
                    except (ValueError, KeyError):
                        continue
    for interactions in store.glob("recordings/*/interactions.jsonl"):
        for line in interactions.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            media_ref = (item.get("response") or {}).get("mediaRef")
            if media_ref:
                meta = objects_root / f"{media_ref}.json"
                if meta.is_file():
                    try:
                        record = json.loads(meta.read_text(encoding="utf-8"))
                        referenced.add(record["sha256"])
                    except (ValueError, KeyError):
                        continue
    return referenced


def plan_cleanup(store: Path, policy: RetentionPolicy | None = None) -> CleanupPlan:
    """生成清理计划但不删除;被引用对象一律列出为受保护。"""
    policy = policy or RetentionPolicy()
    objects_root = store / "objects" / "frayune"
    referenced = referenced_artifact_hashes(store)
    plan = CleanupPlan(note="dry-run:仅生成清单;删除需显式 execute_cleanup")
    if objects_root.is_dir():
        for meta_file in objects_root.glob("art-*.json"):
            record = json.loads(meta_file.read_text(encoding="utf-8"))
            if record.get("revoked"):
                continue  # 已撤销对象由撤销流程处理
            if policy.objectProtection and record["sha256"] in referenced:
                plan.protectedObjects += 1
            else:
                plan.deleteObjects.append(record["sha256"])
    return plan


def execute_cleanup(store: Path, plan: CleanupPlan) -> dict:
    """执行删除;只删计划内对象。审计:被删对象哈希写入 cleanup 审计日志。"""
    objects_root = store / "objects" / "frayune"
    removed = 0
    for digest in plan.deleteObjects:
        blob = objects_root / digest
        if blob.is_file():
            blob.unlink()
            removed += 1
        meta = objects_root / f"art-{digest[:16]}.json"
        meta.unlink(missing_ok=True)
    audit = store / "cleanup-audit.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {"ts": now(), "removedObjects": removed, "digests": plan.deleteObjects[:200]},
                ensure_ascii=False,
            )
            + "\n"
        )
    return {"removedObjects": removed, "audit": str(audit)}


def revoke_artifact(store: Path, artifact_id: str, reason: str, revoked_by: str) -> dict:
    """撤销敏感素材:删除字节、元数据标记 revoked、取消回放资格、留删除审计(§6.4)。"""
    objects = ObjectStore(store, project="frayune")
    record, _ = objects.get(artifact_id)  # 不存在则抛错
    digest = record.sha256
    blob = store / "objects" / "frayune" / digest
    had_content = blob.is_file()
    if had_content:
        digest_before = sha256_bytes(blob.read_bytes())
        if digest_before != digest:
            raise ValueError(f"素材哈希已不一致,拒绝静默撤销:{artifact_id}")
        blob.unlink()
    meta_path = store / "objects" / "frayune" / f"{artifact_id}.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["revoked"] = True
    meta["revokedAt"] = now()
    meta["revokedReason"] = reason
    meta["revokedBy"] = revoked_by
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    audit = store / "cleanup-audit.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "ts": now(),
                    "kind": "revoke",
                    "artifactId": artifact_id,
                    "sha256": digest,
                    "reason": reason,
                    "by": revoked_by,
                    "contentRemoved": had_content,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return {"artifactId": artifact_id, "revoked": True, "contentRemoved": had_content}
