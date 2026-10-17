"""候选导出、清洗、人工批准和批量索引。输出新文件，永不覆盖在线文件。"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps/api'))
from backend.agent.media.intent.embedder import HTTPEmbedder  # noqa: E402
from backend.agent.media.intent.promote import promote  # noqa: E402
from backend.agent.media.intent.reindex import build_index  # noqa: E402
from backend.agent.media.intent.retriever import load_library  # noqa: E402
from backend.agent.media.intent.wash import wash  # noqa: E402


def read_records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        handle.write(value)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export')
    export.add_argument('--output', required=True)
    clean = sub.add_parser('wash')
    clean.add_argument('--candidates', required=True)
    clean.add_argument('--heldout', required=True, help='隔离评测原话 JSON 数组，必须显式提供')
    clean.add_argument('--output', required=True)
    clean.add_argument('--rejected', required=True)
    approve = sub.add_parser('promote')
    approve.add_argument('--library', required=True)
    approve.add_argument('--candidates', required=True)
    approve.add_argument('--heldout', required=True)
    approve.add_argument('--approve', nargs='+', required=True)
    approve.add_argument('--approved-by', required=True)
    approve.add_argument('--output', required=True)
    seeds = sub.add_parser('approve-seeds')
    seeds.add_argument('--library', required=True)
    seeds.add_argument('--heldout', required=True)
    seeds.add_argument('--approve', nargs='+', required=True)
    seeds.add_argument('--approved-by', required=True)
    seeds.add_argument('--model-id', required=True)
    seeds.add_argument('--output', required=True)
    index = sub.add_parser('reindex')
    index.add_argument('--library', required=True)
    index.add_argument('--embed-url', default='http://127.0.0.1:8091/v1/embeddings')
    index.add_argument('--model-id', required=True)
    index.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'wash':
        ready, rejected = wash(read_records(args.candidates), json.loads(Path(args.heldout).read_text()))
        write_new(args.output, ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in ready))
        write_new(args.rejected, ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rejected))
    elif args.command == 'promote':
        result = promote(load_library(args.library), read_records(args.candidates), args.approve,
            args.approved_by, json.loads(Path(args.heldout).read_text()))
        write_new(args.output, json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    elif args.command == 'approve-seeds':
        from backend.agent.media.intent.promote import approve_seeds
        result = approve_seeds(load_library(args.library), args.approve, args.approved_by,
            args.model_id, json.loads(Path(args.heldout).read_text()))
        write_new(args.output, json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    elif args.command == 'reindex':
        result = await build_index(load_library(args.library), HTTPEmbedder(args.embed_url, args.model_id, timeout=30))
        write_new(args.output, json.dumps(result, ensure_ascii=False) + '\n')
    else:
        from sqlalchemy import select
        from backend.agent.orm import AgentRun
        from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
        # 不隐式使用应用数据库；操作者须明确指定可读取的候选来源。
        engine = create_async_database_engine(DatabaseSettings(url=os.environ['INTENT_FEEDBACK_DATABASE_URL']))
        records = {}
        try:
            async with create_async_session_factory(engine)() as db:
                rows = await db.scalars(select(AgentRun).where(AgentRun.phase == 'media'))
                for run in rows:
                    for record in (run.execution or {}).get('intentFeedback', []):
                        if record.get('eligible_after', 0) > time.time():
                            continue
                        if record['source'] == 'task_success':
                            from backend.infrastructure.orm import Document
                            from backend.agent.media.intent.feedback_log import success_stable
                            import uuid
                            document = await db.get(Document, uuid.UUID(run.snapshot['id']))
                            if not success_stable(run, document):
                                continue
                        records[record['id']] = record
            write_new(args.output, ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records.values()))
        finally:
            await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
