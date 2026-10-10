"""Loopback-only mock benchmark fixture server; never reads the real provider config."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid5, NAMESPACE_URL

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=7815)
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
base = Path(tempfile.mkdtemp(prefix='aivideo-benchmark-'))
data = base / 'data'
documents = data / 'documents'
documents.mkdir(parents=True)
config_path = base / 'config.json'
config_path.write_text(json.dumps({'provider': 'mock', 'port': args.port}))
os.environ['SWARMUI_CONFIG'] = str(config_path)
os.environ['SWARMUI_DATA_DIR'] = str(data)
sys.path.insert(0, str(root / 'apps/api'))

from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from backend.evaluation.sandbox import create_sandbox_app as create_app  # noqa: E402
from backend.config import Config  # noqa: E402
import uvicorn  # noqa: E402

scenes = []
for count in (500, 5000):
    name = f'text-{count}'
    ident = str(uuid5(NAMESPACE_URL, f'aivideo-benchmark-v2/{name}'))
    objects = {
        f'perf-{i}': {
            'id': f'perf-{i}', 'kind': 'text', 'x': (i % 25) * 180,
            'y': (i // 25) * 120, 'width': 160, 'height': 100,
            'text': f'对象 {i}', 'fontSize': 16,
        }
        for i in range(count)
    }
    doc = {'id': ident, 'name': name, 'objects': objects, 'order': list(objects), 'revision': 1,
           'createdAt': '2026-01-01T00:00:00Z', 'updatedAt': '2026-01-01T00:00:00Z'}
    encoded = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()
    (documents / f'{ident}.json').write_bytes(encoded)
    scenes.append({'name': name, 'id': ident, 'count': count, 'hash': hashlib.sha256(encoded).hexdigest()})

app = create_app(root=root, data_dir=data, config=Config(provider='mock', port=args.port))


async def page(_request):
    return HTMLResponse((root / 'apps/web/canvas/index.html').read_text())


async def manifest(_request):
    return JSONResponse({'protocol': 'canvas-native-v3', 'scenes': scenes})


app.router.routes[:0] = [Route('/benchmark', page), Route('/benchmark/scenes', manifest)]
print(f'Isolated mock fixture data: {base}', flush=True)
uvicorn.run(app, host='127.0.0.1', port=args.port)
