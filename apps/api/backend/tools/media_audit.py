"""Read-only media recovery verification and orphan inventory. Never deletes files."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time

from sqlalchemy import select

from backend.infrastructure.artifacts import ArtifactSpool
from backend.infrastructure.database import create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Asset, Job


async def audit_media(factory, root: Path, grace_s: float = 7 * 86400) -> dict:
    root = root.resolve()
    spool = ArtifactSpool(root)
    async with factory() as session:
        assets = list(await session.scalars(select(Asset)))
        jobs = list(await session.scalars(select(Job).where(Job.status.in_(["queued", "running", "unknown"]))))
    protected = {str((root / asset.storage_key).resolve().parent) for asset in assets}
    active_ids = {str(job.id) for job in jobs}
    protected.update(str((root / job.phase_payload["storage_key"]).resolve().parent)
        for job in jobs if job.phase_payload.get("storage_key"))
    failures = []
    for asset in assets:
        try:
            await asyncio.to_thread(spool.verify, {"storage_key": asset.storage_key, "sha256": asset.sha256, "bytes": asset.bytes})
        except (OSError, ValueError) as exc:
            failures.append({"assetId": str(asset.id), "error": type(exc).__name__})
    candidates = []
    for folder in (root / "generated").glob("*/*"):
        if (not folder.is_dir() or folder.is_symlink() or not folder.resolve().is_relative_to(root)
                or str(folder.resolve()) in protected or folder.parent.name in active_ids):
            continue
        files = list(folder.iterdir())
        if any(path.is_symlink() for path in files):
            continue
        latest = max([folder.stat().st_mtime, *(path.stat().st_mtime for path in files)])
        if time.time() - latest >= grace_s:
            candidates.append(str(folder.relative_to(root)))
    return {"assetsChecked": len(assets), "failures": failures, "orphanCandidates": sorted(candidates),
        "readOnly": True, "warning": "候选项不是删除授权；在线扫描后需停写并再次核对引用、任务与备份"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-root", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("AIVERO_DB_URL"):
        raise RuntimeError("Explicit AIVERO_DB_URL required")
    async def run():
        engine = create_async_database_engine()
        try:
            report = await audit_media(create_async_session_factory(engine), args.media_root)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if report["failures"] else 0
        finally:
            await engine.dispose()
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
