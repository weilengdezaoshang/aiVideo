"""Cancellation races, slot ownership, and direct-provider API contract."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.cutout import Recognition


def test_cancel_does_not_free_native_slot_or_save_mask(root, png):
    started, release = threading.Event(), threading.Event()

    def infer(source):
        started.set()
        assert release.wait(5)
        return png

    with TestClient(create_app(root, cutout_infer=infer)) as client, ThreadPoolExecutor() as pool:
        ident = client.post("/api/assets?ext=png", content=png).json()["asset"]["id"]
        try:
            request = pool.submit(client.post, f"/api/cutout/detect/{ident}?operationId=cancel-me")
            assert started.wait(3)
            assert client.get("/api/health").status_code == 200
            assert client.delete("/api/cutout/operations/cancel-me").status_code == 200
            assert request.result(timeout=2).status_code == 409
            # The cancelled native thread still owns the slot. No unbounded executor queue.
            busy = client.post(f"/api/cutout/detect/{ident}?operationId=next-operation")
            assert busy.status_code == 429
            assert len(client.app.state.assets.meta) == 1
        finally:
            release.set()


def test_cancel_before_start_and_completed_contract(root, png):
    calls = []

    def infer(source):
        calls.append(source)
        return png

    with TestClient(create_app(root, cutout_infer=infer)) as client:
        ident = client.post("/api/assets?ext=png", content=png).json()["asset"]["id"]
        client.delete("/api/cutout/operations/pre-cancelled")
        client.delete("/api/cutout/operations/pre-cancelled")
        assert (
            client.post(f"/api/cutout/detect/{ident}?operationId=pre-cancelled").status_code == 409
        )
        assert not calls
        result = client.post(f"/api/cutout/detect/{ident}?operationId=live-operation")
        assert result.status_code == 200, result.text
        payload = result.json()
        assert payload["operationId"] == "live-operation"
        assert payload["provider"] == "rembg"
        assert client.get(payload["urls"]["original"]).content == png
        assert len(calls) == 1
        assert not client.app.state.recognition.active
        assert client.post(f"/api/cutout/detect/{ident}?operationId=bad").status_code == 400


def test_timeout_and_failure_release_only_finished_inference():
    async def scenario():
        release = threading.Event()
        service = Recognition("test", lambda _: release.wait(5) or b"", timeout=0.01)
        try:
            with pytest.raises(HTTPException) as timeout:
                await service.detect("timeout-operation", b"source")
            assert timeout.value.status_code == 504
            service.finish("timeout-operation")
            with pytest.raises(HTTPException) as busy:
                await service.detect("busy-operation", b"source")
            assert busy.value.status_code == 429
        finally:
            release.set()
            service.close()

        def fail(_source):
            raise RuntimeError("inference failed")

        service = Recognition("test", fail)
        try:
            with pytest.raises(HTTPException) as failure:
                await service.detect("failure-operation", b"source")
            assert failure.value.status_code == 502
        finally:
            service.finish("failure-operation")
            service.close()

    asyncio.run(scenario())


def test_duplicate_does_not_remove_first_operation(root, png):
    started, release = threading.Event(), threading.Event()

    def infer(_source):
        started.set()
        assert release.wait(5)
        return png

    with TestClient(create_app(root, cutout_infer=infer)) as client, ThreadPoolExecutor() as pool:
        ident = client.post("/api/assets?ext=png", content=png).json()["asset"]["id"]
        try:
            request = pool.submit(
                client.post, f"/api/cutout/detect/{ident}?operationId=duplicate-id"
            )
            assert started.wait(3)
            assert (
                client.post(f"/api/cutout/detect/{ident}?operationId=duplicate-id").status_code
                == 409
            )
            assert "duplicate-id" in client.app.state.recognition.active
            client.delete("/api/cutout/operations/duplicate-id")
            assert request.result(timeout=2).status_code == 409
        finally:
            release.set()


def test_cancel_during_persistence_removes_only_new_mask(root, png):
    saving, release = threading.Event(), threading.Event()
    with (
        TestClient(create_app(root, cutout_infer=lambda _: png)) as client,
        ThreadPoolExecutor() as pool,
    ):
        assets = client.app.state.assets
        ident = client.post("/api/assets?ext=png", content=png).json()["asset"]["id"]
        original_save = assets.save

        def delayed_save(*args):
            record = original_save(*args)
            saving.set()
            assert release.wait(5)
            return record

        assets.save = delayed_save
        try:
            request = pool.submit(
                client.post, f"/api/cutout/detect/{ident}?operationId=saving-mask"
            )
            assert saving.wait(3)
            client.delete("/api/cutout/operations/saving-mask")
            release.set()
            assert request.result(timeout=2).status_code == 409
            assert list(assets.meta) == [ident]
            assert [path.name for path in assets.root.iterdir()] == [ident]
        finally:
            release.set()
