"""Loopback port probes shared by process management and the project registry."""
import asyncio


async def port_open(port, timeout=.5):
    """True when something accepts TCP connections on this loopback port."""
    for host in ('127.0.0.1', '::1'):
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, TimeoutError):
            continue
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True
    return False
