"""The single application entrypoint: database-backed API and durable workers.

backend.production remains an import-compatible name for this same factory.
The historical in-memory HTTP fixture is isolated under evaluation.sandbox.
"""

from backend.paths import ROOT
from backend.production import create_app

__all__ = ["ROOT", "create_app"]
