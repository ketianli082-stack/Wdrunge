"""Regenerate the TypeScript API client from the checked-in OpenAPI contract."""


from app.agents.tools.validation import tool, failure_result, command_failure

from app.agents.tools._workspace import resolve_allowed
from app.agents.tools._process import run_process


@tool
async def refresh_frontend_client() -> dict:
    """Run npm run api:generate to refresh frontend/src/generated types and SDK."""
    directory = resolve_allowed("frontend", ("frontend",))
    if not (directory / "package.json").is_file():
        return {**failure_result('Frontend API generation unavailable in this workspace; inspect its generation instructions.',
            'environment_unavailable', 'unsupported_project_layout',
            'Inspect this project\'s package scripts and API generation instructions; use a supported authorized command instead of retrying this fixed-layout tool.',
            'after_environment_change'), 'passed': False}
    code, output = await run_process(("npm", "run", "api:generate"), directory)
    return {'exit_code': code, 'passed': code == 0, 'output': output,
            **(command_failure(f'Command exited {code}', output) if code != 0 else {})}
