"""List files visible to one specialist."""

from app.agents.tools.validation import tool

from app.agents.tools._workspace import effective_roots, workspace_root
from app.agents.tools.file_management import walk_files, visible
from itertools import islice


def make_list_project_files(allowed_roots: tuple[str, ...]):
    @tool("list_project_files")
    def list_project_files() -> str:
        """List up to 150 files in your assigned project areas."""
        files: list[str] = []
        workspace = workspace_root().resolve()
        for root in effective_roots(allowed_roots):
            directory = (workspace / root).resolve()
            if not directory.is_relative_to(workspace):
                continue
            if directory.is_file() and visible(directory.relative_to(workspace)):
                files.append(str(directory.relative_to(workspace)))
                continue
            if not directory.exists():
                continue
            if not directory.is_dir():
                continue
            for path in islice(walk_files(directory), max(0, 150-len(files))):
                if path.resolve().is_relative_to(workspace):
                    files.append(str(path.relative_to(workspace)))
        return "\n".join(sorted(set(files))[:150]) or "No files found."

    return list_project_files
