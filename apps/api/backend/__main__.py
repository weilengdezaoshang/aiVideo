"""python -m backend [--reload] [--env-file .env.bailian.local]"""

import argparse
import os

import uvicorn
from dotenv import load_dotenv

from .app import ROOT
from .config import load_config
from .observability import setup_logging

parser = argparse.ArgumentParser()
parser.add_argument("--reload", action="store_true")
parser.add_argument("--env-file")
args = parser.parse_args()
if args.env_file:
    load_dotenv(args.env_file, override=False)
if args.reload:
    os.environ.setdefault("SWARMUI_ENV", "development")
# 结构化 stdout(§九.7):SWARMUI_LOG_FORMAT=json 时输出 JSON 行,由部署层收集。
setup_logging(os.environ.get("LOG_LEVEL", "INFO").upper(), os.environ.get("SWARMUI_LOG_FORMAT", "text"))
production = os.environ.get("AIVERO_API_MODE") == "production"
port = int(os.environ.get("SWARMUI_PORT", "7801")) if production else load_config(ROOT).port
uvicorn.run(
    "backend.production:create_app" if production else "backend.app:create_app",
    factory=True,
    host=os.environ.get("SWARMUI_HOST", "127.0.0.1"),
    port=port,
    reload=args.reload,
    # SSE connections remain open; bound shutdown so reload can finish.
    timeout_graceful_shutdown=3,
    reload_dirs=[str(ROOT / "apps/api/backend")] if args.reload else None,
    log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    # Our structured access log omits query strings (OIDC codes, signed URLs).
    access_log=not production,
)
