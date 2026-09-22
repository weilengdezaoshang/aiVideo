"""Immutable, fenced artifact spool. Recovery reads bytes, never resubmits generation."""

import hashlib
import json
import os
from pathlib import Path
import uuid

from backend.providers.base import Generated


class ArtifactSpool:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def save(self, job_id: uuid.UUID, epoch: int, generated: Generated) -> dict:
        if generated.ext not in {"png", "jpg", "jpeg", "webp", "mp4", "webm", "mkv", "gif", "wav"}:
            raise ValueError("unsupported generated media type")
        if not generated.data:
            raise ValueError("empty generated artifact")
        folder = self.root / "generated" / str(job_id) / str(epoch)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / ("original." + generated.ext)
        self._atomic(target, generated.data)
        record = {"storage_key": str(target.relative_to(self.root)), "ext": generated.ext,
                  "bytes": len(generated.data), "sha256": hashlib.sha256(generated.data).hexdigest()}
        self._atomic(folder / "receipt.json", json.dumps(record).encode())
        return record

    def save_file(self, job_id: uuid.UUID, epoch: int, source: Path, ext: str) -> dict:
        if ext not in {"png", "jpg", "jpeg", "webp", "mp4", "webm", "wav", "gif", "mkv"}:
            raise ValueError("unsupported export type")
        folder = self.root / "generated" / str(job_id) / str(epoch)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / ("original." + ext)
        temporary = folder / (uuid.uuid4().hex + ".tmp")
        digest, size = hashlib.sha256(), 0
        try:
            with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if not size:
                raise ValueError("empty export")
            record = {"storage_key": str(target.relative_to(self.root)), "ext": ext,
                      "bytes": size, "sha256": digest.hexdigest()}
            if target.exists():
                self.verify(record)
            else:
                os.replace(temporary, target)
            self._atomic(folder / "receipt.json", json.dumps(record).encode())
            return record
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, job_id: uuid.UUID, epoch: int) -> dict | None:
        receipt = self.root / "generated" / str(job_id) / str(epoch) / "receipt.json"
        if not receipt.is_file():
            return None
        record = json.loads(receipt.read_text())
        self.verify(record)
        return record

    def verify(self, record: dict) -> None:
        """Verify again before database publication; receipts are not proof of availability."""
        path = (self.root / record["storage_key"]).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("invalid artifact path")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        if size != record["bytes"] or digest.hexdigest() != record["sha256"]:
            raise ValueError("artifact checksum mismatch")

    @staticmethod
    def _atomic(target: Path, data: bytes) -> None:
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
            if os.name == "posix":
                fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)
