"""Run one of the project's fixed verification commands."""

import asyncio
import sys
from typing import Literal

from langchain_core.tools import tool

from app.tools._workspace import WORKSPACE_ROOT


@tool
async def run_project_check(check: Literal["backend_tests", "frontend_build"]) -> str:
    """Run backend pytest or the frontend TypeScript/Vite build and return its result."""
    if check == "backend_tests":
        command = (sys.executable, "-m", "pytest", "-q")
        directory = WORKSPACE_ROOT / "backend"
    else:
        command = ("npm", "run", "build")
        directory = WORKSPACE_ROOT / "frontend"

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=directory,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=120)
    except TimeoutError:
        process.kill()
        await process.communicate()
        return f"{check} timed out after 120 seconds."
    text = output.decode(errors="replace")[-10000:]
    return f"{check} exited {process.returncode}\n{text}"
