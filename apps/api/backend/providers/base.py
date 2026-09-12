from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from ..models import GenParams, InitImage


@dataclass
class Generated:
    data: bytes
    ext: str


class Provider(ABC):
    name = ""
    capacity = 1

    @abstractmethod
    async def status(self) -> dict: ...

    @abstractmethod
    async def models(self) -> list[dict]: ...

    async def samplers(self) -> dict:
        return {"samplers": ["euler"], "schedulers": ["normal"]}

    @abstractmethod
    async def generate(
        self,
        params: GenParams,
        seed: int,
        progress: Callable,
        image: InitImage | None = None,
        mask: InitImage | None = None,
        external: Callable[[str], None] | None = None,
        external_task_id: str | None = None,
    ) -> Generated: ...

    async def query_external(self, task_id: str) -> dict:
        """查询上游异步任务状态;仅云端视频等外部任务型后端实现。"""
        raise ValueError("当前后端不支持查询上游任务")

    async def cancel_external(self, task_id: str) -> bool:
        """尽力取消上游异步任务;返回 False 表示未确认停止。"""
        return False
