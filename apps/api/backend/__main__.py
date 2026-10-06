"""Start the single database-backed API; --reload only controls code reloading."""

import argparse
import os

import uvicorn
from dotenv import load_dotenv

from .paths import ROOT
from .observability import setup_logging

def main(argv=None):
    parser = argparse.ArgumentParser(description="FRAYUNE PostgreSQL / Celery backend")
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--env-file", help="显式部署配置文件；不读取 config.json")
    args = parser.parse_args(argv)
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    setup_logging(os.environ.get("LOG_LEVEL", "INFO").upper(),
                  os.environ.get("SWARMUI_LOG_FORMAT", "text"))
    uvicorn.run(
        "backend.app:create_app",
        factory=True,
        host=os.environ.get("SWARMUI_HOST", "127.0.0.1"),
        port=int(os.environ.get("SWARMUI_PORT", "7801")),
        reload=args.reload,
        # SSE connections remain open; bound shutdown so reload can finish.
        timeout_graceful_shutdown=3,
        reload_dirs=[str(ROOT / "apps/api/backend")] if args.reload else None,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        # Structured access logs omit query strings (OIDC codes, signed URLs).
        access_log=False,
    )


if __name__ == "__main__":
    main()
