"""Regenerate the TypeScript API client from the checked-in OpenAPI contract."""

import asyncio

from langchain_core.tools import tool

from app.tools._workspace import WORKSPACE_ROOT


@tool
async def refresh_frontend_client() -> str:
    """Run npm run api:generate to refresh frontend/src/generated types and SDK."""
    process = await asyncio.create_subprocess_exec(
        "npm",
        "run",
        "api:generate",
        cwd=WORKSPACE_ROOT / "frontend",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return f"Frontend API generation exited {process.returncode}\n{output.decode(errors='replace')[-5000:]}"
