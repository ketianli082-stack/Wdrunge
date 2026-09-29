"""List files visible to one specialist."""

from langchain_core.tools import tool

from app.tools._workspace import WORKSPACE_ROOT


def make_list_project_files(allowed_roots: tuple[str, ...]):
    @tool("list_project_files")
    def list_project_files() -> str:
        """List up to 150 files in your assigned project areas."""
        files: list[str] = []
        for root in allowed_roots:
            directory = WORKSPACE_ROOT / root
            if directory.is_file():
                files.append(root)
                continue
            if not directory.exists():
                continue
            for path in directory.rglob("*"):
                if path.is_file() and not any(
                    part in {".git", ".venv", "node_modules", "dist", "__pycache__"}
                    for part in path.parts
                ):
                    files.append(str(path.relative_to(WORKSPACE_ROOT)))
        return "\n".join(sorted(set(files))[:150]) or "No files found."

    return list_project_files
