"""Durable fake service. Its SQLite database survives worker processes and restarts.

No application tables, credentials, network services or paid calls are used here.
"""
import io
import base64
import wave
import sqlite3
import time
import uuid
from pathlib import Path

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from .contracts import ProviderResult


# Self-generated one-frame MP4 fixture; no remote assets or inference.
TINY_MP4 = 'AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAMVbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAAMgAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAkB0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAAMgAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAEAAAABAAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAADIAAAAAAABAAAAAAG4bWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAoAAAACABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABY21pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAASNzdGJsAAAAv3N0c2QAAAAAAAAAAQAAAK9hdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAEAAQABIAAAASAAAAAAAAAABFExhdmM2My4xLjEwMiBsaWJ4MjY0AAAAAAAAAAAAAAAAGP//AAAANWF2Y0MBZAAK/+EAGGdkAAqs2UQmwEQAAAMABAAAAwAoPEiWWAEABmjr48siwP34+AAAAAAQcGFzcAAAAAEAAAABAAAAFGJ0cnQAAAAAAAByOAAAAAAAAAAYc3R0cwAAAAAAAAABAAAAAQAACAAAAAAcc3RzYwAAAAAAAAABAAAAAQAAAAEAAAABAAAAFHN0c3oAAAAAAAAC2wAAAAEAAAAUc3RjbwAAAAAAAAABAAADRQAAAGF1ZHRhAAAAWW1ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAG1kaXJhcHBsAAAAAAAAAAAAAAAALGlsc3QAAAAkqXRvbwAAABxkYXRhAAAAAQAAAABMYXZmNjMuMS4xMDIAAAAIZnJlZQAAAuNtZGF0AAACrQYF//+p3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2NSByMzIyMiBiMzU2MDVhIC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyNSAtIGh0dHA6Ly93d3cudmlkZW9sYW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTEgcmVmPTMgZGVibG9jaz0xOjA6MCBhbmFseXNlPTB4MzoweDExMyBtZT1oZXggc3VibWU9NyBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0xIG1lX3JhbmdlPTE2IGNocm9tYV9tZT0xIHRyZWxsaXM9MSA4eDhkY3Q9MSBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0tMiB0aHJlYWRzPTIgbG9va2FoZWFkX3RocmVhZHM9MSBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRlcmxhY2VkPTAgYmx1cmF5X2NvbXBhdD0wIGNvbnN0cmFpbmVkX2ludHJhPTAgYmZyYW1lcz0zIGJfcHlyYW1pZD0yIGJfYWRhcHQ9MSBiX2JpYXM9MCBkaXJlY3Q9MSB3ZWlnaHRiPTEgb3Blbl9nb3A9MCB3ZWlnaHRwPTIga2V5aW50PTI1MCBrZXlpbnRfbWluPTUgc2NlbmVjdXQ9NDAgaW50cmFfcmVmcmVzaD0wIHJjX2xvb2thaGVhZD00MCByYz1jcmYgbWJ0cmVlPTEgY3JmPTIzLjAgcWNvbXA9MC42MCBxcG1pbj0wIHFwbWF4PTY5IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MToxLjAwAIAAAAAmZYiEAD///uZ1+BTTCBpJMvxDzj8xVynnAWznTQ4fVSgMZ1FwY28='


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    outcome: str = Field(default="success", pattern="^(success|timeout|retryable|blocked_by_model|param_invalid|provider_down)$")
    complete_after_seconds: float = Field(default=0, ge=0)
    link_ttl_seconds: float = Field(default=60, gt=0)
    idempotent: bool = True
    query_registration: bool = True
    cancellable: bool = True
    callback_copies: int = Field(default=1, ge=0)
    actual_credits: int = Field(default=1, ge=0)
    database_timeout_seconds: float = Field(default=5, gt=0)
    download_failures: int = Field(default=0, ge=0)


class FakeProvider:
    def __init__(self, path: Path, scenario: Scenario):
        self.path, self.scenario = path, scenario
        path.parent.mkdir(parents=True, exist_ok=True)
        self.idempotent, self.query_registration, self.cancellable = scenario.idempotent, scenario.query_registration, scenario.cancellable
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, registration TEXT, idem TEXT, config TEXT, created REAL, cancelled INTEGER DEFAULT 0, downloads INTEGER DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS calls (operation TEXT, task_id TEXT)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=self.scenario.database_timeout_seconds)
        db.row_factory = sqlite3.Row
        return db

    async def submit(self, registration_id, params, idempotency_key):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM tasks WHERE idem=?", (idempotency_key,)).fetchone() if self.idempotent and idempotency_key else None
            if old:
                return self.result(old)
            ident = str(uuid.uuid4())
            db.execute("INSERT INTO tasks(id,registration,idem,config,created) VALUES(?,?,?,?,?)",
                       (ident, registration_id, idempotency_key, self.scenario.model_dump_json(), time.time()))
            db.execute("INSERT INTO calls VALUES('submit',?)", (ident,))
            row = db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
        if self.scenario.outcome == "timeout":
            raise TimeoutError("fake accepted submission but response was lost")
        return self.result(row)

    def result(self, row):
        config = Scenario.model_validate_json(row["config"])
        if row["cancelled"]:
            return ProviderResult("cancelled", row["id"])
        ready = row["created"] + config.complete_after_seconds
        if time.time() < ready:
            return ProviderResult("pending", row["id"])
        if config.outcome not in {"success", "timeout"}:
            return ProviderResult("failed", row["id"], failure="retryable" if config.outcome == "retryable" else config.outcome)
        return ProviderResult("succeeded", row["id"], locator={"fake_id": row["id"]},
                              expires_at=ready + config.link_ttl_seconds, actual_credits=config.actual_credits)

    async def query(self, task_id, registration_id):
        if not task_id and not self.query_registration:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone() if task_id else db.execute(
                "SELECT * FROM tasks WHERE registration=? ORDER BY created LIMIT 1", (registration_id,)).fetchone()
            return self.result(row) if row else None

    async def download(self, locator, kind):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (locator["fake_id"],)).fetchone()
            if row is None or time.time() >= self.result(row).expires_at:
                raise ValueError("fake result expired")
            config = Scenario.model_validate_json(row["config"])
            db.execute("UPDATE tasks SET downloads=downloads+1 WHERE id=?", (row["id"],))
            failed = row["downloads"] < config.download_failures
        if failed:
            raise OSError("fake temporary storage failure")
        if kind == "video":
            return base64.b64decode(TINY_MP4), "mp4"
        if kind == "audio":
            audio = io.BytesIO()
            with wave.open(audio, "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(8000)
                stream.writeframes(b"\x00\x00" * 80)
            return audio.getvalue(), "wav"
        image = io.BytesIO()
        Image.new("L" if kind == "mask" else "RGB", (64, 64), 255 if kind == "mask" else "red").save(image, format="PNG")
        return image.getvalue(), "png"

    async def cancel(self, task_id):
        if not self.cancellable:
            return await self.query(task_id, "")
        current = await self.query(task_id, "")
        if current and current.status != "pending":
            return current
        with self.connect() as db:
            db.execute("UPDATE tasks SET cancelled=1 WHERE id=?", (task_id,))
        return await self.query(task_id, "")

    async def callbacks(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM tasks").fetchall()
        return [(r["id"], self.result(r)) for r in rows for _ in range(Scenario.model_validate_json(r["config"]).callback_copies)
                if self.result(r).status != "pending"]

    def counts(self):
        with self.connect() as db:
            return {"submissions": db.execute("SELECT count(*) FROM calls WHERE operation='submit'").fetchone()[0],
                    "tasks": db.execute("SELECT count(*) FROM tasks").fetchone()[0]}


def create_fake_app(path: Path, scenario: Scenario):
    """Optional independent HTTP host for other blocks' black-box tests."""
    from dataclasses import asdict
    from fastapi import FastAPI
    from fastapi.responses import Response
    app, provider = FastAPI(), FakeProvider(path, scenario)
    def wire(result):
        return asdict(result) if result else None
    @app.post("/tasks")
    async def submit(body: dict):
        return wire(await provider.submit(body["registration_id"], body["params"], body.get("idempotency_key")))
    @app.get("/tasks")
    async def query(registration_id: str):
        return wire(await provider.query(None, registration_id))
    @app.get("/tasks/{ident}")
    async def get(ident: str):
        return wire(await provider.query(ident, ""))
    @app.post("/tasks/{ident}/cancel")
    async def cancel(ident: str):
        return wire(await provider.cancel(ident))
    @app.get("/artifacts/{ident}")
    async def artifact(ident: str, kind: str = "image"):
        data, ext = await provider.download({"fake_id": ident}, kind)
        return Response(data, media_type={"mp4": "video/mp4", "wav": "audio/wav"}.get(ext, "image/png"))
    @app.get("/callbacks")
    async def callbacks():
        return [wire(result) for _, result in await provider.callbacks()]
    return app
