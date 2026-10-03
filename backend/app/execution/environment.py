"""Detect project manifests; dependency commands never target global environments."""
import json
import os
from pathlib import Path
import sys
from app.execution.file_ops import read_regular
from app.agents.tools.validation import ToolFailure


def manifest_text(path):
    data = read_regular(path, 2 * 1024**2)
    if len(data) > 2 * 1024**2:
        raise ValueError('Project manifest exceeds 2 MiB')
    return data.decode('utf-8')


def inspect_environment(directory: Path):
    result = {'directory': str(directory), 'runtimes': []}
    if (directory / 'package.json').is_file():
        package = json.loads(manifest_text(directory / 'package.json'))
        manager = str(package.get('packageManager', '')).split('@')[0]
        if manager not in {'npm', 'pnpm', 'yarn', 'bun'}:
            manager = next((manager for lock, manager in [('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn'), ('bun.lock', 'bun'), ('bun.lockb', 'bun')] if (directory / lock).exists()), 'npm')
        result['runtimes'].append({'type': 'node', 'manager': manager, 'engines': package.get('engines', {}),
            'scripts': package.get('scripts', {}), 'dependencies_installed': (directory / 'node_modules').is_dir(),
            'dependencies_shared': (directory / 'node_modules').is_symlink()})
    if (directory / 'pyproject.toml').is_file() or (directory / 'requirements.txt').is_file():
        import tomllib
        pyproject = tomllib.loads(manifest_text(directory / 'pyproject.toml')) if (directory / 'pyproject.toml').exists() else {}
        result['runtimes'].append({'type': 'python', 'manager': 'uv' if (directory / 'uv.lock').exists() else 'venv',
            'requires_python': pyproject.get('project', {}).get('requires-python'),
            'environment': str(directory / '.venv'), 'installed': project_python(directory).exists()})
    for manifest, runtime in [('Cargo.toml', 'rust'), ('go.mod', 'go'), ('Gemfile', 'ruby'), ('composer.json', 'php')]:
        if (directory / manifest).is_file():
            result['runtimes'].append({'type': runtime, 'manifest': manifest, 'installation': 'Use reviewed execute_command with project-local configuration'})
    return result


def project_python(directory):
    return directory / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def verification_python(directory):
    """Prefer the task environment, or its source project's read-only environment."""
    from app.agents.tools._workspace import source_workspace, workspace_root
    local = project_python(directory)
    if local.exists():
        return local
    source = source_workspace.get()
    if source:
        shared = project_python(source / directory.relative_to(workspace_root()))
        if shared.exists():
            return shared
    raise ToolFailure('项目尚未安装 Python 环境；先用 detect_environment 检查环境，再通过 execute_command 创建项目 .venv 并安装依赖',
        'environment_unavailable', 'missing_project_python',
        'Use detect_environment for the target project, then prepare its .venv and locked dependencies through authorized execute_command; do not borrow the backend host environment.',
        'after_environment_change')


def installation_commands(directory, runtime):
    entries = inspect_environment(directory)['runtimes']
    item = next((entry for entry in entries if entry['type'] == runtime), None)
    if not item:
        raise ValueError(f'No {runtime} manifest in this directory')
    # Refuse installation through a shared snapshot dependency link.
    target = directory / ('node_modules' if runtime == 'node' else '.venv')
    if target.is_symlink() or not target.resolve().is_relative_to(directory.resolve()):
        raise ValueError('依赖目录指向工作区外；请先在独立工作区准备自己的依赖目录，不能覆盖共享环境')
    if runtime == 'node':
        manager = item['manager']
        return [[manager, 'ci' if manager == 'npm' and (directory / 'package-lock.json').exists() else 'install']]
    if runtime != 'python':
        raise ValueError('Automatic installation currently supports Python and Node manifests')
    if item['manager'] == 'uv':
        return [['uv', 'sync', '--frozen']]
    commands = []
    python = project_python(directory)
    if not python.exists():
        commands.append([sys.executable, '-m', 'venv', str(directory / '.venv')])
    commands.append([str(python), '-m', 'pip', 'install', *(['-r', 'requirements.txt'] if (directory / 'requirements.txt').exists() else ['-e', '.'])])
    return commands
