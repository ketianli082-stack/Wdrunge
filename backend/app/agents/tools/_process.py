"""Compatibility entrypoint: fixed checks use the reviewed sandboxed runtime too."""
from app.agents.tools._workspace import workspace_root
from app.execution.processes import processes


async def run_process(command, directory, timeout=120):
    relative = str(directory.resolve().relative_to(workspace_root().resolve()))
    result = await processes.run(list(command), relative, timeout, reason='Run the project verification command')
    return result['exit_code'], result['output'] + ('\nCommand timed out' if result['timed_out'] else '')
