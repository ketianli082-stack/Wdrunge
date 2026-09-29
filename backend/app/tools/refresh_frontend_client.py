"""Regenerate the TypeScript API client from the checked-in OpenAPI contract."""

import asyncio

from langchain_core.tools import tool

from app.tools._workspace import resolve_allowed


@tool
async def refresh_frontend_client() -> str:
    """Run npm run api:generate to refresh frontend/src/generated types and SDK."""
    directory = resolve_allowed("frontend", ("frontend",))
    if not (directory / "package.json").is_file():
        return "Frontend API generation unavailable in this workspace."
    process = await asyncio.create_subprocess_exec(
        "npm",
        "run",
        "api:generate",
        cwd=directory,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return f"Frontend API generation exited {process.returncode}\n{output.decode(errors='replace')[-5000:]}"
