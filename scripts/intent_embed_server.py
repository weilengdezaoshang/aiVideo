"""本机多语向量侧车，只加载显式的本地模型快照，不下载模型。"""
import asyncio
import os
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, StringConstraints


class EmbedRequest(BaseModel):
    model: str
    input: list[Annotated[str, StringConstraints(min_length=1, max_length=9000)]] = Field(min_length=1, max_length=64)


def create_app(model=None, model_id=None):
    ident = model_id or os.environ.get('INTENT_EMBED_MODEL_ID')
    if not ident or ident == 'pending':
        raise RuntimeError('必须固定 INTENT_EMBED_MODEL_ID')
    if model is None:
        root = Path(os.environ['INTENT_LOCAL_MODEL_PATH']).resolve(strict=True)
        if not root.is_dir():
            raise RuntimeError('INTENT_LOCAL_MODEL_PATH 必须是已下载的固定模型目录')
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(str(root), local_files_only=True, device='cpu')
    app = FastAPI(title='aiVideo 本地意图向量侧车')
    lock = asyncio.Lock()

    @app.post('/v1/embeddings')
    async def embed(request: EmbedRequest):
        if request.model != ident:
            raise HTTPException(400, '请求的模型版本与侧车固定版本不一致')
        async with lock:
            task = asyncio.create_task(asyncio.to_thread(model.encode, request.input, normalize_embeddings=True))
            try:
                vectors = await asyncio.shield(task)
            except asyncio.CancelledError:
                # HTTP 取消不能杀死原生线程；实际完成前不能释放模型并发槽位。
                await task
                raise
        return {'model': ident, 'data': [{'embedding': vector} for vector in vectors.tolist()]}

    return app
