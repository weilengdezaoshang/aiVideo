"""A loopback-only TCP fault gate, never a proxy for arbitrary external endpoints."""

import asyncio


class DatabaseProxy:
    def __init__(self, host, port):
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("fault proxy requires a loopback test database")
        self.host, self.target_port, self.available = host, port, True
        self.writers = set()
        self.tasks = set()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.connection, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def connection(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        if not self.available:
            writer.close()
            self.tasks.discard(task)
            return
        upstream = None
        try:
            other, upstream = await asyncio.open_connection(self.host, self.target_port)
            self.writers.update((writer, upstream))

            async def pipe(source, target):
                while chunk := await source.read(65536):
                    target.write(chunk)
                    await target.drain()
                target.close()

            await asyncio.gather(pipe(reader, upstream), pipe(other, writer))
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            if upstream:
                upstream.close()
            self.writers.discard(writer)
            self.writers.discard(upstream)
            self.tasks.discard(task)

    async def disconnect(self):
        self.available = False
        for writer in list(self.writers):
            writer.transport.abort()
        await asyncio.sleep(0)

    async def __aexit__(self, *args):
        await self.disconnect()
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
