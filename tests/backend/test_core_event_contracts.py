import asyncio
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from backend.core.events.contracts import ClaimCheck, EventSettings, EventType, partition
from backend.core.events.registry import Registry
from backend.core.events.store import HandlerTransaction


def test_complete_event_enum_and_claim_check():
    assert len(EventType) == 18
    claim = ClaimCheck(event_id=uuid.uuid4(), run_id=uuid.uuid4())
    assert ClaimCheck.model_validate(claim.model_dump(mode="json")) == claim
    with pytest.raises(ValidationError):
        ClaimCheck.model_validate({**claim.model_dump(), "prompt": "forbidden"})


def test_partition_is_stable_and_distributed():
    ids = [uuid.UUID(int=i) for i in range(100)]
    assert [partition(i, 10) for i in ids] == [partition(i, 10) for i in ids]
    assert set(partition(i, 10) for i in ids) == set(range(10))
    with pytest.raises(ValueError):
        partition(ids[0], 0)


def test_backoff_is_bounded_and_configured():
    settings = EventSettings(backoff_base_seconds=2, backoff_max_seconds=10, backoff_jitter=0)
    assert [settings.delay(i) for i in (1, 2, 3, 4, 100000)] == [2, 4, 8, 10, 10]
    with pytest.raises(ValidationError):
        EventSettings(max_attempts=0)
    with pytest.raises(ValidationError):
        EventSettings(backoff_base_seconds=10, backoff_max_seconds=1)


def test_registry_requires_versioned_async_unique_handlers():
    registry = Registry()
    async def handler(tx, context):
        pass
    registry.register(EventType.TIMEOUT, "v1", handler)
    assert registry.resolve(SimpleNamespace(event_kind="domain", event_type="timeout")).version == "v1"
    assert registry.resolve(SimpleNamespace(event_kind="state")).version == "state-v1"
    with pytest.raises(ValueError):
        registry.register(EventType.TIMEOUT, "v1", handler)
    with pytest.raises(ValueError):
        registry.register(EventType.USER_MESSAGE, "", handler)
    with pytest.raises(ValueError):
        registry.register(EventType.USER_MESSAGE, "v1", lambda *_: None)


def test_handler_cannot_commit_or_control_transaction():
    async def scenario():
        from sqlalchemy import text
        tx = HandlerTransaction(None)
        for action in (tx.commit(), tx.rollback(), tx.execute(text("COMMIT")), tx.execute(text("DROP TABLE x"))):
            with pytest.raises(ValueError):
                await action
    asyncio.run(scenario())


def test_handler_loader_uses_explicit_trusted_registration(monkeypatch):
    from types import ModuleType
    import sys
    module = ModuleType("core_events_test_plugin")
    async def handler(tx, context):
        pass
    module.register = lambda registry: registry.register(EventType.TIMEOUT, "plugin-v1", handler)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    registry = Registry().load([module.__name__])
    assert registry.resolve(SimpleNamespace(event_kind="domain", event_type="timeout")).version == "plugin-v1"


def test_timer_backoff_is_positive_capped_and_configured():
    from backend.core.events.contracts import timer_delay
    config = EventSettings(timer_backoff_base_seconds=.1, timer_backoff_max_seconds=.4,
        timer_backoff_jitter=.2)
    for skip in (1, 2, 3, 100000):
        value = timer_delay(config, skip)
        assert 0 < value <= .4
    assert .32 <= timer_delay(config, 1000) <= .4
    with pytest.raises(ValidationError):
        EventSettings(timer_backoff_jitter=1)
    with pytest.raises(ValidationError):
        EventSettings(timer_backoff_base_seconds=2, timer_backoff_max_seconds=1)


def test_nonblocking_lock_distinguishes_absent_from_busy():
    from backend.core.events.locking import locked, LockBusy
    from backend.core.orm import CoreRun
    class Session:
        def __init__(self, value):
            self.value = value
        async def scalar(self, statement):
            return None
        async def get(self, *args, **kwargs):
            return self.value
    async def scenario():
        ident = uuid.uuid4()
        assert await locked(Session(None), CoreRun, ident) is None
        with pytest.raises(LockBusy):
            await locked(Session(object()), CoreRun, ident)
    asyncio.run(scenario())
