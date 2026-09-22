import asyncio
import socket

import httpcore
import pytest

from backend.infrastructure.public_http import PublicNetworkBackend, create_public_client


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "2002:7f00:1::", "ff02::1"])
def test_artifact_tcp_rejects_private_and_tunnel_destinations(monkeypatch, address):
    async def scenario():
        async def resolve(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        with pytest.raises(httpcore.ConnectError, match="not public"):
            await PublicNetworkBackend().connect_tcp("cdn.test", 443, timeout=1)
    asyncio.run(scenario())


def test_artifact_connect_uses_verified_ip_not_second_dns_lookup(monkeypatch):
    calls = []
    class TCP:
        async def connect_tcp(self, host, port, **kwargs):
            calls.append((host, port))
            return "test-stream"
    async def scenario():
        async def resolve(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        stream = await PublicNetworkBackend(TCP()).connect_tcp("cdn.test", 443, timeout=1)
        assert stream == "test-stream" and calls == [("1.1.1.1", 443)]
        client = create_public_client()
        assert isinstance(client._transport._pool._network_backend, PublicNetworkBackend)
        await client.aclose()
    asyncio.run(scenario())
