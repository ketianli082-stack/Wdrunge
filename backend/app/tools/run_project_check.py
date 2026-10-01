"""Run one of the project's fixed verification commands."""

import sys
from typing import Literal

from langchain_core.tools import tool

from app.tools._workspace import resolve_allowed
from app.tools._process import run_process


@tool
async def run_project_check(check: Literal["backend_tests", "frontend_build"]) -> str:
    """Run backend pytest or the frontend TypeScript/Vite build and return its result."""
    if check == "backend_tests":
        command = (sys.executable, "-m", "pytest", "-q")
        directory = resolve_allowed("backend", ("backend",))
    else:
        command = ("npm", "run", "build")
        directory = resolve_allowed("frontend", ("frontend",))

    if not directory.is_dir():
        return f"{check} unavailable: {directory} does not exist."

    code, output = await run_process(command, directory)
    return f"{check} exited {code}\n{output}"
