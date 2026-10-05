"""Regenerate the OpenAPI contract after backend endpoint changes."""

from app.execution.environment import verification_python

from app.agents.tools.validation import tool, failure_result, command_failure

from app.agents.tools._workspace import resolve_allowed
from app.agents.tools._process import run_process


@tool
async def refresh_api_contract() -> dict:
    """Export FastAPI's current OpenAPI schema to backend/openapi.json."""
    directory = resolve_allowed("backend", ("backend",))
    if not (directory / "scripts" / "export_openapi.py").is_file():
        return {**failure_result('OpenAPI export unavailable in this workspace; inspect its export instructions.',
            'environment_unavailable', 'unsupported_project_layout',
            'Inspect this project\'s actual OpenAPI export/entrypoint instructions and use an authorized project-specific command; repeating this fixed-layout tool will not help.',
            'after_environment_change'), 'passed': False}
    code, output = await run_process((str(verification_python(directory)), "-m", "scripts.export_openapi"), directory)
    return {'exit_code': code, 'passed': code == 0, 'output': output,
            **(command_failure(f'Command exited {code}', output) if code != 0 else {})}
