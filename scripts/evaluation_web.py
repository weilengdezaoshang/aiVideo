"""显式隔离的本地评测工作台，不作为生产应用入口。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if sys.prefix == sys.base_prefix:
    python = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    os.execv(str(python), [str(python), __file__])
sys.path[0] = str(ROOT / 'apps/api')
from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from backend.evaluation.api import build_eval_router  # noqa: E402
import uvicorn  # noqa: E402

app = FastAPI(title='本地评测工作台')
app.include_router(build_eval_router(ROOT / 'data' / 'eval-web'))
for name in ('brand', 'ui'):
    app.mount('/' + name, StaticFiles(directory=ROOT / 'apps/web' / name))
# 前端只读编译产物；评测证据只落独立目录。

@app.get('/')
def index():
    from fastapi.responses import HTMLResponse
    return HTMLResponse((ROOT / 'apps/web/evaluation/index.html').read_text())

@app.get('/evaluation/evaluation.css')
def css():
    from fastapi.responses import FileResponse
    return FileResponse(ROOT / 'apps/web/evaluation/evaluation.css')

app.mount('/evaluation', StaticFiles(directory=ROOT / '.web-build/evaluation'), name='evaluation-js')

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8788)
