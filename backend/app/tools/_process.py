"""Bounded subprocesses that stop their process group when a task is cancelled."""
import asyncio
import os
import signal


async def run_process(command, directory, timeout=120):
    process = await asyncio.create_subprocess_exec(*command, cwd=directory,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=os.name != 'nt')
    async def stop():
        if process.returncode is None:
            try:
                if os.name != 'nt':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        return process.returncode, output.decode(errors='replace')[-10000:]
    except TimeoutError:
        await stop()
        return -1, f'Timed out after {timeout} seconds'
    except asyncio.CancelledError:
        await stop()
        raise
