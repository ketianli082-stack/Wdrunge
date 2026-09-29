"""Regenerate the OpenAPI contract after backend endpoint changes."""

import asyncio
import sys

from langchain_core.tools import tool

from app.tools._workspace import WORKSPACE_ROOT


@tool
async def refresh_api_contract() -> str:
    """Export FastAPI's current OpenAPI schema to backend/openapi.json."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.export_openapi",
        cwd=WORKSPACE_ROOT / "backend",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return f"OpenAPI export exited {process.returncode}\n{output.decode(errors='replace')[-5000:]}"
