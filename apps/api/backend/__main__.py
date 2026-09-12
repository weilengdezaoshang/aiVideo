"""python -m backend [--reload] [--env-file .env.bailian.local]"""

import argparse
import logging
import os

import uvicorn
from dotenv import load_dotenv

from .app import ROOT
from .config import load_config

parser = argparse.ArgumentParser()
parser.add_argument("--reload", action="store_true")
parser.add_argument("--env-file")
args = parser.parse_args()
if args.env_file:
    load_dotenv(args.env_file, override=False)
if args.reload:
    os.environ.setdefault("SWARMUI_ENV", "development")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
config = load_config(ROOT)
uvicorn.run(
    "backend.app:create_app",
    factory=True,
    host=os.environ.get("SWARMUI_HOST", "127.0.0.1"),
    port=config.port,
    reload=args.reload,
    # SSE connections remain open; bound shutdown so reload can finish.
    timeout_graceful_shutdown=3,
    reload_dirs=[str(ROOT / "apps/api/backend")] if args.reload else None,
    log_level=os.environ.get("LOG_LEVEL", "info").lower(),
)
