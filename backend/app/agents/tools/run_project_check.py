"""Run one of the project's fixed verification commands."""

from app.execution.environment import verification_python
from typing import Literal

from app.agents.tools.validation import tool, failure_result, command_failure

from app.agents.tools._workspace import resolve_allowed
from app.agents.tools._process import run_process


@tool
async def run_project_check(check: Literal["backend_tests", "frontend_build"]) -> dict:
    """Run backend pytest or the frontend TypeScript/Vite build and return its result."""
    if check == "backend_tests":
        directory = resolve_allowed("backend", ("backend",))
    else:
        directory = resolve_allowed("frontend", ("frontend",))

    if not directory.is_dir():
        return {**failure_result(f'{check} unavailable: project directory does not exist',
            'environment_unavailable', 'unsupported_project_layout',
            'Inspect the actual project layout and test/build commands; use a supported authorized command.',
            'after_environment_change'), 'passed': False}

    command = (str(verification_python(directory)), "-m", "pytest", "-q") if check == 'backend_tests' else ("npm", "run", "build")
    code, output = await run_process(command, directory)
    return {'check': check, 'exit_code': code, 'passed': code == 0, 'output': output,
            **(command_failure(f'Command exited {code}', output) if code != 0 else {})}
