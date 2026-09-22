"""In-process rembg inference. Cancellation invalidates results, never kills native threads."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from fastapi import HTTPException
from PIL import Image


class Rembg:
    def __init__(self, model: str):
        self.model = model
        self.session = None

    def __call__(self, source: bytes) -> bytes:
        # Import and model loading happen lazily on the inference thread, not the API loop.
        from rembg import new_session, remove

        if self.session is None:
            self.session = new_session(self.model)
        output = remove(source, session=self.session, alpha_matting=False)
        with Image.open(io.BytesIO(output)) as image:
            buffer = io.BytesIO()
            image.convert("RGBA").getchannel("A").save(buffer, format="PNG")
            return buffer.getvalue()


class Recognition:
    def __init__(self, model: str, infer: Callable[[bytes], bytes] | None = None, timeout=120):
        self.model = model
        self.infer = infer or Rembg(model)
        self.timeout = timeout
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rembg")
        self.running = None
        self.active: dict[str, asyncio.Event] = {}
        # Short-lived tombstones cover DELETE arriving before POST; these are not mask caches.
        self.cancelled: OrderedDict[str, float] = OrderedDict()

    def cancel(self, ident: str):
        self.prune()
        self.cancelled[ident] = time.monotonic()
        self.cancelled.move_to_end(ident)
        while len(self.cancelled) > 2048:
            self.cancelled.popitem(last=False)
        if event := self.active.get(ident):
            event.set()

    def prune(self):
        while self.cancelled and next(iter(self.cancelled.values())) < time.monotonic() - 300:
            self.cancelled.popitem(last=False)

    def check(self, ident: str):
        if ident in self.cancelled or (ident in self.active and self.active[ident].is_set()):
            raise HTTPException(409, "主体识别已取消")

    async def detect(self, ident: str, source: bytes) -> bytes:
        self.prune()
        self.check(ident)
        if ident in self.active:
            raise HTTPException(409, "识别请求已存在")
        # Do not release this slot when an HTTP request is cancelled: native inference still runs.
        if self.running is not None and not self.running.done():
            raise HTTPException(429, "主体识别正在处理中，请稍后重试")
        event = self.active[ident] = asyncio.Event()
        future = self.executor.submit(self.infer, source)
        self.running = future
        result = asyncio.wrap_future(future)
        # Consume exceptions even when the client left before inference completed.
        result.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        cancelled = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait(
                [result, cancelled], timeout=self.timeout, return_when=asyncio.FIRST_COMPLETED
            )
            self.check(ident)
            if result not in done:
                raise HTTPException(504, "主体识别超时，请稍后重试")
            mask = result.result()
            if not mask:
                raise ValueError("empty mask")
            return mask
        except HTTPException:
            raise
        except Exception:
            logging.exception("rembg 主体识别失败")
            raise HTTPException(502, "主体识别失败，请检查 rembg 依赖与模型文件后重试") from None
        finally:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)

    def finish(self, ident: str):
        self.active.pop(ident, None)

    def close(self):
        for event in self.active.values():
            event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
