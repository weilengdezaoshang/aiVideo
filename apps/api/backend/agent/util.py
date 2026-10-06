"""Small pure helpers shared by the Agent kernel, loop policies and service."""
from datetime import datetime, timezone
import json


def now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.isoformat() if value else None


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def error(code, message):
    return {"code": code, "message": message}


def public_error(value):
    return value.get("message") if isinstance(value, dict) else value
