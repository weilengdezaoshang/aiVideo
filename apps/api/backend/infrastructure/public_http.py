"""Public artifact pool: validate DNS and connect to the validated IP, preserving TLS SNI.

The provider pool is separate: explicit ComfyUI endpoints may be internal. Untrusted
artifact origins cannot opt into that exception or use environment proxy settings.
"""
import asyncio
import ipaddress
import socket

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, backend=None):
        self.backend = backend or AutoBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        async def connect():
            addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            if not addresses:
                raise httpcore.ConnectError("Artifact DNS returned no addresses")
            ips = []
            for entry in addresses:
                address = ipaddress.ip_address(entry[4][0])
                if not address.is_global or address.is_multicast or address.is_reserved or (address.version == 6 and (
                        address.ipv4_mapped or address.sixtofour or address.teredo)):
                    raise httpcore.ConnectError("Artifact destination is not public")
                if str(address) not in ips:
                    ips.append(str(address))
            # Never resolve the original name again. httpcore retains the original
            # origin for Host and certificate/SNI verification above this TCP layer.
            error = None
            for address in ips:
                try:
                    return await self.backend.connect_tcp(address, port, timeout=timeout,
                        local_address=local_address, socket_options=socket_options)
                except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                    error = exc
            raise error
        try:
            async with asyncio.timeout(timeout):
                return await connect()
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("Artifact connection deadline exceeded") from exc
        except socket.gaierror as exc:
            raise httpcore.ConnectError("Artifact DNS unavailable") from exc

    async def connect_unix_socket(self, *args, **kwargs):
        raise httpcore.ConnectError("Artifact Unix sockets are forbidden")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def create_public_client():
    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5))
    # HTTPX 0.28/httpcore 1.0 have no public backend injection hook. Keep this
    # version-locked compatibility seam isolated and fail closed on API drift.
    if not isinstance(transport._pool, httpcore.AsyncConnectionPool) or not hasattr(transport._pool, "_network_backend"):
        raise RuntimeError("Unsupported artifact HTTP transport version")
    transport._pool._network_backend = PublicNetworkBackend()
    return httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
