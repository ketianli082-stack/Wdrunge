"""Regenerate the TypeScript API client from the checked-in OpenAPI contract."""


from app.tools.validation import tool

from app.tools._workspace import resolve_allowed
from app.tools._process import run_process


@tool
async def refresh_frontend_client() -> str:
    """Run npm run api:generate to refresh frontend/src/generated types and SDK."""
    directory = resolve_allowed("frontend", ("frontend",))
    if not (directory / "package.json").is_file():
        return "Frontend API generation unavailable in this workspace."
    code, output = await run_process(("npm", "run", "api:generate"), directory)
    return f"Frontend API generation exited {code}\n{output}"
