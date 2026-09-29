"""Regenerate the OpenAPI contract after backend endpoint changes."""

import asyncio
import sys

from langchain_core.tools import tool

from app.tools._workspace import resolve_allowed


@tool
async def refresh_api_contract() -> str:
    """Export FastAPI's current OpenAPI schema to backend/openapi.json."""
    directory = resolve_allowed("backend", ("backend",))
    if not (directory / "scripts" / "export_openapi.py").is_file():
        return "OpenAPI export unavailable in this workspace."
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.export_openapi",
        cwd=directory,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return f"OpenAPI export exited {process.returncode}\n{output.decode(errors='replace')[-5000:]}"
