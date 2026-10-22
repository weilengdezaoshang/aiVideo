"""Inspect or explicitly publish deployment-owned tool definitions, without provider calls."""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[0] = str(ROOT)
sys.path.insert(0, str(ROOT / "apps/api"))



def main():
    from backend.agent.media.catalog import Catalog
    from backend.core.tools import build_catalog
    from backend.core.tools.inventory import inventory_markdown
    from backend.core.tools.store import publish
    from backend.infrastructure.database import create_async_database_engine, create_async_session_factory

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-catalog", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--publish", action="store_true", help="Explicitly write AIVERO_DB_URL; otherwise print inventory only")
    parser.add_argument("--published-by")
    args = parser.parse_args()
    catalog = build_catalog(Catalog.load(args.host_catalog), args.version)
    if not args.publish:
        print(inventory_markdown(catalog), end="")
        return
    if not args.published_by:
        parser.error("--publish requires --published-by")
    # Publishing requires an explicit URL, never the database module's fallback.
    import os
    if not os.environ.get("AIVERO_DB_URL"):
        parser.error("--publish requires an explicit AIVERO_DB_URL")
    async def write():
        engine = create_async_database_engine()
        try:
            async with create_async_session_factory(engine).begin() as tx:
                lock = await publish(tx, catalog, published_by=args.published_by)
            print(lock.model_dump_json())
        finally:
            await engine.dispose()
    asyncio.run(write())


if __name__ == "__main__":
    main()
