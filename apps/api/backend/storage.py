"""Disk layout compatible with the original server; mutations run on one owner thread."""

from __future__ import annotations

import io
import json
import re
import secrets
import wave
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps

from .common import now, read_json, valid_id, write_json
from .errors import DataCorruptedError
from .timeline import validate_timeline
from .storyboard import validate_storyboard

# 历史产物文件名 img_{24hex}.{ext};宽松匹配宁可误保留,漏判会让画布产物丢失。
_HISTORY_FILE_RE = re.compile(r"img_[0-9a-f]{8,}\.[a-z0-9]{2,5}", re.IGNORECASE)


def referenced_history_files(root: Path) -> set[str] | None:
    """扫描画布文档,返回仍被引用的历史产物文件名;任一文档不可读时返回 None。

    画布对象以 src=/images/img_xxx.ext 直连历史产物(generation-reducer 的
    imageRecordToAsset),文本扫描覆盖 objects/groups/chat 任意字段,不依赖对象结构。
    返回 None 表示引用状态未知,调用方必须保留文件,不得静默当作无引用。
    """
    documents = root / "documents"
    if not documents.is_dir():
        return set()
    referenced: set[str] = set()
    for path in documents.glob("*.json"):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        referenced.update(_HISTORY_FILE_RE.findall(text))
    return referenced


class Conflict(ValueError):
    def __init__(self, document):
        super().__init__("文档已被其他会话更新")
        self.document = document


class Documents:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def get(self, ident):
        if not valid_id(ident):
            return None
        doc = read_json(self.root / f"{ident}.json", None)
        return doc if isinstance(doc, dict) and doc.get("id") == ident else None

    def create(self, name=None, key=None):
        mapping = read_json(self.root / "_idempotency.json", {})
        existing = self.get(mapping.get(key, {}).get("docId", "")) if key else None
        if existing:
            return existing
        doc = dict(
            id=str(uuid4()), name=self.name(name), objects={}, order=[], revision=1, updatedAt=now()
        )
        write_json(self.root / f"{doc['id']}.json", doc)
        if key:
            mapping[key] = dict(docId=doc["id"], createdAt=now())
            mapping = dict(
                sorted(mapping.items(), key=lambda x: x[1]["createdAt"], reverse=True)[:500]
            )
            write_json(self.root / "_idempotency.json", mapping)
        return doc

    @staticmethod
    def name(value):
        return value.strip()[:100] if isinstance(value, str) and value.strip() else "未命名画布"

    def save(self, ident, raw):
        current = self.get(ident)
        if current is None:
            return None
        if not isinstance(raw.get("objects"), dict) or len(raw["objects"]) > 5000:
            raise ValueError("文档 objects 必须是对象且不超过 5000 项")
        if not isinstance(raw.get("order"), list) or any(
            not isinstance(x, str) for x in raw["order"]
        ):
            raise ValueError("order 必须是对象 id 数组")
        if "baseRevision" in raw and raw["baseRevision"] != current["revision"]:
            raise Conflict(current)
        doc = dict(
            id=ident,
            name=self.name(raw.get("name")),
            objects=raw["objects"],
            order=raw["order"],
            revision=current["revision"] + 1,
            updatedAt=now(),
        )
        for key, kind, maximum in [
            ("groups", dict, 200),
            ("chat", list, 500),
            ("generations", dict, 5000),
        ]:
            if key in raw:
                if not isinstance(raw[key], kind) or len(raw[key]) > maximum:
                    raise ValueError(f"文档 {key} 格式错误或超出限制")
                doc[key] = raw[key]
        if "generations" not in raw and "generations" in current:
            doc["generations"] = current["generations"]
        if "storyboard" in raw:
            if raw["storyboard"] is not None:
                doc["storyboard"] = validate_storyboard(raw["storyboard"])
        elif "storyboard" in current:
            doc["storyboard"] = current["storyboard"]
        if "timeline" in raw:
            if raw["timeline"] is not None:
                doc["timeline"] = validate_timeline(raw["timeline"])
        elif "timeline" in current:
            doc["timeline"] = current["timeline"]
        if "schemaVersion" in raw:
            doc["schemaVersion"] = raw["schemaVersion"]
        if len(json.dumps(doc).encode()) > 5 * 1024**2:
            raise ValueError("文档内容超过大小限制")
        write_json(self.root / f"{ident}.json", doc)
        return doc

    def rename(self, ident, name):
        doc = self.get(ident)
        if doc:
            doc.update(name=self.name(name), revision=doc["revision"] + 1, updatedAt=now())
            write_json(self.root / f"{ident}.json", doc)
        return doc

    def remove(self, ident):
        if not self.get(ident):
            return False
        (self.root / f"{ident}.json").unlink()
        return True

    def list(self):
        docs = [self.get(p.stem) for p in self.root.glob("*.json") if not p.name.startswith("_")]
        return sorted(
            [
                dict(
                    id=d["id"],
                    name=d["name"],
                    updatedAt=d["updatedAt"],
                    objectCount=len(d["order"]),
                )
                for d in docs
                if d
            ],
            key=lambda d: d["updatedAt"],
            reverse=True,
        )


class Assets:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.meta = {}
        for p in root.glob("*/meta.json"):
            record = read_json(p, {})
            if valid_id(p.parent.name) and record.get("id") == p.parent.name:
                self.meta[record["id"]] = record

    def get(self, ident):
        return self.meta.get(ident)

    def urls(self, ident):
        r = self.meta[ident]
        poster = None
        poster_id = r.get("posterAssetId")
        poster_record = self.meta.get(poster_id) if poster_id else None
        if poster_record:
            poster = f"/assets/{poster_id}/original.{poster_record['ext']}"
        return dict(
            original=f"/assets/{ident}/original.{r['ext']}",
            thumb256=f"/assets/{ident}/t256.webp" if r["hasThumbs"] else None,
            thumb1024=f"/assets/{ident}/t1024.webp" if r["hasThumbs"] else None,
            poster=poster,
        )

    def read(self, ident):
        record = self.get(ident)
        if (
            not record
            or not valid_id(ident)
            or record["ext"] not in {"jpg", "jpeg", "png", "webp", "mp4", "webm", "wav"}
        ):
            return None
        file = self.root / ident / f"original.{record['ext']}"
        return (record, file.read_bytes()) if file.exists() else None

    def save(self, data, ext, kind="image", name=None):
        ext = ext.lower().lstrip(".")
        allowed = {
            "video": {"mp4", "webm"},
            "image": {"jpg", "jpeg", "png", "webp"},
            "audio": {"wav"},
        }.get(kind, set())
        if ext not in allowed:
            raise ValueError("不支持的素材格式")
        ext = "jpg" if ext == "jpeg" else ext
        width = height = 0
        image = None
        audio_meta = {}
        if kind == "audio":
            try:
                with wave.open(io.BytesIO(data), "rb") as audio:
                    frames, rate = audio.getnframes(), audio.getframerate()
                    channels, width_bytes = audio.getnchannels(), audio.getsampwidth()
                    if (
                        not 1 <= channels <= 2
                        or width_bytes not in (1, 2, 3, 4)
                        or not 8000 <= rate <= 192000
                        or not 0 < frames / rate <= 3600
                    ):
                        raise ValueError("音频参数不受支持")
                    if len(audio.readframes(frames)) != frames * channels * width_bytes:
                        raise ValueError("音频数据不完整")
                    audio_meta = dict(durationSec=frames / rate, sampleRate=rate, channels=channels)
            except (wave.Error, EOFError, ValueError) as exc:
                raise ValueError("请上传有效的单声道或双声道 PCM WAV 音频，最长一小时") from exc
        if kind == "image":
            try:
                image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
                image.load()
                width, height = image.size
            except Exception as exc:
                raise ValueError("无法解码图片") from exc
        ident = str(uuid4())
        directory = self.root / ident
        directory.mkdir()
        (directory / f"original.{ext}").write_bytes(data)
        if image:
            for size in (256, 1024):
                thumb = image.copy()
                thumb.thumbnail((size, size))
                thumb.save(directory / f"t{size}.webp", "WEBP", quality=82)
        record = dict(
            id=ident,
            kind=kind,
            ext=ext,
            width=width,
            height=height,
            hasThumbs=image is not None,
            createdAt=now(),
        )
        record.update(audio_meta)
        if name:
            record["name"] = str(name).replace("\\", "/").rsplit("/", 1)[-1][:200]
        write_json(directory / "meta.json", record)
        self.meta[ident] = record
        return record

    def remove(self, ident):
        # Only internally generated asset identifiers are accepted here.
        import shutil

        if valid_id(ident) and self.meta.pop(ident, None):
            shutil.rmtree(self.root / ident)

    def list(self, kind=None, limit=200):
        return sorted(
            [r for r in self.meta.values() if not kind or r["kind"] == kind],
            key=lambda r: r.get("createdAt", ""),
            reverse=True,
        )[:limit]

    def update_meta(self, ident, **fields):
        """补写视频时长/画幅/海报等元数据;只接受白名单字段,缺失资产返回 None。"""
        record = self.meta.get(ident)
        if not record or not valid_id(ident):
            return None
        for key, value in fields.items():
            if key in {"durationMs", "width", "height", "posterAssetId"}:
                record[key] = value
        write_json(self.root / ident / "meta.json", record)
        return record


class History:
    def __init__(self, root: Path):
        self.root = root
        (root / "images").mkdir(parents=True, exist_ok=True)
        history_path = root / "history.json"
        if not history_path.exists():
            self.records = []
            return
        # 数据损坏不静默回空:回空后下一次 persist 会覆盖原文件,损失无法恢复。
        try:
            payload = json.loads(history_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataCorruptedError("历史记录文件损坏,已停止加载,请从备份恢复后重试") from exc
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise DataCorruptedError("历史记录文件结构异常,已停止加载,请从备份恢复后重试")
        self.records = [r for r in records if isinstance(r, dict)]

    def persist(self):
        write_json(self.root / "history.json", {"records": self.records})

    def list(self, limit=100, q=None, model=None, kind=None, starred=None):
        result = []
        for r in self.records:
            p = r["params"]
            if kind and p.get("kind") != kind or model and p.get("model") != model:
                continue
            if starred is not None and bool(r.get("starred")) != starred:
                continue
            if q and q.lower() not in (p.get("prompt", "") + p.get("negativePrompt", "")).lower():
                continue
            result.append(r)
        return result[:limit]

    def save(self, job_id, provider, params, data, ext):
        if not ext.isalnum():
            raise ValueError("非法生成文件格式")
        file = f"img_{secrets.token_hex(12)}.{ext}"
        (self.root / "images" / file).write_bytes(data)
        record = dict(
            id=str(uuid4()),
            jobId=job_id,
            file=file,
            url=f"/images/{file}",
            provider=provider,
            params=params,
            createdAt=now(),
        )
        self.records.insert(0, record)
        referenced = referenced_history_files(self.root)
        count = 0
        for r in list(self.records):
            if not r.get("starred"):
                count += 1
                if count > 500:
                    self.records.remove(r)
                    self._drop_file(r, referenced)
        self.persist()
        return record

    def _drop_file(self, record, referenced):
        """引用状态未知或文件仍被画布引用时只裁历史索引,保留产物文件。"""
        if referenced is not None and record["file"] not in referenced:
            self.delete_file(record)

    def delete_file(self, record):
        filename = record["file"]
        if Path(filename).name == filename and ".." not in filename:
            (self.root / "images" / filename).unlink(missing_ok=True)

    def remove(self, ident):
        record = next((r for r in self.records if r["id"] == ident), None)
        if record:
            referenced = referenced_history_files(self.root)
            self.records.remove(record)
            self._drop_file(record, referenced)
            self.persist()
        return record

    def star(self, ident, starred):
        record = next((r for r in self.records if r["id"] == ident), None)
        if record:
            record["starred"] = starred
            self.persist()
        return record


def apply_mask(original: bytes, mask: bytes) -> bytes:
    image = Image.open(io.BytesIO(original)).convert("RGB")
    alpha = Image.open(io.BytesIO(mask))
    if alpha.format != "PNG":
        raise ValueError("蒙版必须是 PNG")
    if image.size != alpha.size:
        raise ValueError("Mask 尺寸必须与原图一致")
    image.putalpha(alpha.convert("L"))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()
