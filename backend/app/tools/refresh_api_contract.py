"""Regenerate the OpenAPI contract after backend endpoint changes."""

import sys

from langchain_core.tools import tool

from app.tools._workspace import resolve_allowed
from app.tools._process import run_process


@tool
async def refresh_api_contract() -> str:
    """Export FastAPI's current OpenAPI schema to backend/openapi.json."""
    directory = resolve_allowed("backend", ("backend",))
    if not (directory / "scripts" / "export_openapi.py").is_file():
        return "OpenAPI export unavailable in this workspace."
    code, output = await run_process((sys.executable, "-m", "scripts.export_openapi"), directory)
    return f"OpenAPI export exited {code}\n{output}"
