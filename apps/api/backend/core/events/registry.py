"""Versioned, trusted local handlers. Only database effects belong in handlers."""
from dataclasses import dataclass
import importlib
import inspect
from typing import Awaitable, Callable

from backend.core.orm import CoreEvent, CoreRun, CoreTask
from .contracts import EventType
from .store import HandlerTransaction


@dataclass(frozen=True)
class Context:
    event: CoreEvent
    run: CoreRun
    task: CoreTask | None


@dataclass(frozen=True)
class Handler:
    version: str
    callback: Callable[[HandlerTransaction, Context], Awaitable[None]]


async def bookkeeping(tx, context):
    pass


class Registry:
    def __init__(self):
        self.handlers: dict[EventType, Handler] = {}

    def register(self, event_type, version, callback):
        event_type = EventType(event_type)
        if event_type in self.handlers:
            raise ValueError(f"duplicate handler: {event_type}")
        if not version or len(version) > 128 or not inspect.iscoroutinefunction(callback):
            raise ValueError("handler needs a bounded version and async callback")
        self.handlers[event_type] = Handler(version, callback)

    def resolve(self, event):
        if event.event_kind == "state":
            return Handler("state-v1", bookkeeping)
        return self.handlers.get(EventType(event.event_type))

    def load(self, modules):
        for module in modules:
            importlib.import_module(module).register(self)
        return self
