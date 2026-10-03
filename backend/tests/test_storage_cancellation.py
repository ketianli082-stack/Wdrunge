"""Cancelling a task in the middle of a write must not leave SQLite locked."""
import asyncio
import time

import pytest
from sqlalchemy import text

from app.collaboration.service import Collaboration
from app.storage import ChatStore

# Slow enough (~0.3-1 s) that a cancellation lands while the statement runs in aiosqlite.
SLOW = text('WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3000000) SELECT count(*) FROM c')


async def cancel_mid_statement(write):
    started = asyncio.Event()
    async def writer():
        async with write() as connection:
            await connection.execute(text("UPDATE conversations SET title='changed'"))
            started.set()
            await connection.execute(SLOW)
    task = asyncio.create_task(writer())
    await started.wait()
    await asyncio.sleep(.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize('path', ['store', 'collaboration'])
def test_cancelled_write_releases_the_database_lock(tmp_path, path):
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "cancel.db"}')
        project = await store.create_project('P', str(tmp_path))
        conversation = (await store.create_conversation(project['id']))['id']
        service = Collaboration(store)
        await service.initialize()
        await cancel_mid_statement(store.write if path == 'store' else service.transaction)
        # The cancelled transaction rolled back; the next writer is not locked out
        # (an invalidated connection would hold the lock past the 5 s busy timeout).
        started = time.monotonic()
        await store.add_event(conversation, 'probe', {})
        assert time.monotonic() - started < 2
        async with store.engine.connect() as connection:
            titles = (await connection.execute(text('SELECT title FROM conversations'))).scalars().all()
        assert 'changed' not in titles
        await store.close()
    asyncio.run(scenario())


def test_cancelled_caller_releases_a_resource_it_will_never_receive():
    from app.storage import complete
    released = []
    async def scenario():
        async def connect():
            await asyncio.sleep(.1)
            return 'connection'
        async def release(connection):
            released.append(connection)
        task = asyncio.create_task(complete(connect(), release=release))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert released == ['connection']
