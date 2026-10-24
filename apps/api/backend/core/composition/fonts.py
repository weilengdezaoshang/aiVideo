"""Pinned Noto Sans SC shipped beside this package. No docs/ path and no download."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from PIL import ImageFont

from .cmap import covered_codepoints
from .errors import CompositionError

_DIR = Path(__file__).resolve().parent / "fonts"
_SOURCE = json.loads((_DIR / "source.json").read_text(encoding="utf-8"))
_FILE = _SOURCE["files"]["NotoSansSC-Regular.otf"]

FONT_ID = "noto-sans-sc"
FONT_VERSION = _SOURCE["upstream_commit"]
FONT_SHA256 = _FILE["sha256"]
FONT_PATH = _DIR / "NotoSansSC-Regular.otf"
MAX_FONT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class FontFace:
    id: str
    version: str
    sha256: str
    path: Path
    face_index: int
    max_bytes: int = MAX_FONT_BYTES

    def load(self, size: int) -> ImageFont.FreeTypeFont:
        self._verified()
        return ImageFont.truetype(self.path, size=size, index=self.face_index)

    @cached_property
    def cmap(self) -> frozenset[int]:
        self._verified()
        return frozenset(covered_codepoints(self.path))

    def require_coverage(self, copy: str) -> None:
        """Whitespace and newlines need no glyph. Every other codepoint must be in cmap."""
        covered = self.cmap
        for char in copy:
            if char in "\n\r\t" or unicodedata.category(char).startswith("Z"):
                continue
            if ord(char) not in covered:
                raise CompositionError("missing_glyph")

    def _verified(self) -> None:
        """One sfnt face. A collection or a non-zero index is unsupported, not another face."""
        if self.face_index != 0:
            raise CompositionError("unsupported_collection")
        path = self.path.resolve()
        if not path.is_file():
            raise CompositionError("unknown_font")
        if path.stat().st_size > self.max_bytes:
            raise CompositionError("font_path_rejected")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            prefix = handle.read(4)
            if prefix == b"ttcf":
                raise CompositionError("unsupported_collection")
            digest.update(prefix)
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        if digest.hexdigest() != self.sha256:
            raise CompositionError("font_hash_mismatch")


def locked_faces(snapshot: dict, registry: FontRegistry) -> FontRegistry:
    """Use the published id/version/sha. A newer file with the same id is a mismatch."""
    faces = []
    for item in snapshot.get("fonts") or []:
        face = registry.get(str(item.get("id")))
        if (face.sha256 != item.get("sha256") or face.version != item.get("version")
                or face.face_index != int(item.get("face_index", 0))):
            raise CompositionError("font_hash_mismatch")
        faces.append(face)
    if not faces:
        raise CompositionError("unknown_font")
    return FontRegistry(faces, registry.roots)


class FontRegistry:
    def __init__(self, faces: list[FontFace], roots: list[Path]):
        self._faces = {face.id: face for face in faces}
        self.roots = [root.resolve() for root in roots]

    def ids(self) -> list[str]:
        return sorted(self._faces)

    def get(self, font_id: str) -> FontFace:
        face = self._faces.get(font_id)
        if face is None:
            raise CompositionError("unknown_font")
        path = face.path.resolve()
        if path.is_symlink() or face.path.is_symlink():
            raise CompositionError("font_path_rejected")
        if not any(path.is_relative_to(root) for root in self.roots):
            raise CompositionError("font_path_rejected")
        face._verified()
        return face

    def public_catalog(self) -> list[dict[str, str]]:
        return [{"id": face.id, "version": face.version, "sha256": face.sha256}
                for face in (self._faces[key] for key in self.ids())]


def bundled_registry(max_bytes: int = MAX_FONT_BYTES) -> FontRegistry:
    face = FontFace(FONT_ID, FONT_VERSION, FONT_SHA256, FONT_PATH, 0, max_bytes)
    return FontRegistry([face], [_DIR])


def registry_from_file(path: Path, max_bytes: int = MAX_FONT_BYTES) -> FontRegistry:
    raw = json.loads(path.read_text(encoding="utf-8"))
    root = Path(raw["root"]).resolve()
    if not root.is_dir():
        raise CompositionError("font_path_rejected")
    faces = []
    for item in raw["fonts"]:
        font_path = (root / item["path"]).resolve()
        if not font_path.is_relative_to(root):
            raise CompositionError("font_path_rejected")
        faces.append(FontFace(id=item["id"], version=str(item["version"]), sha256=item["sha256"],
                              path=font_path, face_index=int(item.get("face_index", 0)), max_bytes=max_bytes))
    return FontRegistry(faces, [root])


def load_registry(path: str, max_bytes: int = MAX_FONT_BYTES) -> FontRegistry:
    if not path:
        return bundled_registry(max_bytes)
    return registry_from_file(Path(path), max_bytes)
