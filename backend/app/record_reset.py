"""Drain local API activity before a debug-only record reset."""
import asyncio
from starlette.responses import JSONResponse


class RecordsGate:
    def __init__(self):
        self.resetting = False
        self.reset_task = None
        self.requests = set()

    async def drain(self):
        pending = list(self.requests)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


class RecordsGateMiddleware:
    def __init__(self, app, gate):
        self.app, self.gate = app, gate

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/api/') or scope['path'] == '/api/debug/records/reset':
            return await self.app(scope, receive, send)
        if self.gate.resetting:
            return await JSONResponse({'detail': '正在清除记录，请稍后重试'}, status_code=503)(scope, receive, send)
        task = asyncio.current_task()
        self.gate.requests.add(task)
        try:
            await self.app(scope, receive, send)
        finally:
            self.gate.requests.discard(task)
