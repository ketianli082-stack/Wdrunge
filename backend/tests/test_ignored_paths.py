"""A project's .gitignore decides what is source: ignored paths are neither copied into
task snapshots nor merged back."""
import os
import shutil
import subprocess

import pytest

from app.collaboration.workspaces import Ignore, IsolatedWorkspace, unmerged_reason


def write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


TREE = {
    '.gitignore': '# caches\n*.log\n/build/\n.vite\n!keep.log\ndocs/**/draft-*\n\\#notes\nsrc/gen/**\n!src/gen/**/\ntmp?/\n[Tt]humbs.db\n*//\n',
    'app.log': '', 'keep.log': '', 'main.py': '', '#notes': '', 'Thumbs.db': '',
    'build/out.js': '', 'pkg/build/out.js': '', 'tmp1/x': '', 'tmp12/x': '',
    'docs/a/b/draft-1.md': '', 'docs/final.md': '', 'docs/draft-0.md': '',
    'src/gen/a.py': '', 'src/gen/deep/b.py': '', 'src/main.py': '',
    'frontend/.gitignore': 'dist-*\n!dist-keep\n/local.json\n*.log\n!important.log\n[a\\]]x\n',
    'frontend/.vite/deps/a.js': '', 'frontend/src/.vite/x': '', 'frontend/src/App.vue': '',
    'frontend/dist-1/x': '', 'frontend/dist-keep/x': '', 'frontend/local.json': '', 'frontend/src/local.json': '',
    'frontend/important.log': '', 'frontend/keep.log': '', 'frontend/other.log': '', 'frontend/]x': '', 'frontend/ax': '',
}


@pytest.mark.skipif(not shutil.which('git'), reason='git is not installed')
def test_the_rules_decide_what_git_decides(tmp_path):
    write(tmp_path, TREE)
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
    subprocess.run(['git', 'init', '-q', str(tmp_path)], env=env, check=True)
    paths = sorted(name for name in TREE if not name.endswith('.gitignore'))
    out = subprocess.run(['git', '-c', f'core.excludesFile={os.devnull}', 'check-ignore', '--stdin', '-z'], cwd=tmp_path, env=env,
                         input='\0'.join(paths).encode(), capture_output=True, check=False).stdout.decode()
    ignore = Ignore(tmp_path)
    assert sorted(path for path in paths if ignore.reason(path)) == sorted(path for path in out.split('\0') if path)


def test_the_rules_without_git(tmp_path):
    write(tmp_path, TREE)
    ignore = Ignore(tmp_path)
    ignored = {path for path in TREE if not path.endswith('.gitignore') and ignore.reason(path)}
    assert ignored == {
        'app.log', '#notes', 'Thumbs.db', 'build/out.js', 'tmp1/x', 'docs/a/b/draft-1.md', 'docs/draft-0.md',
        'src/gen/a.py', 'src/gen/deep/b.py', 'frontend/.vite/deps/a.js', 'frontend/src/.vite/x', 'frontend/dist-1/x',
        'frontend/local.json', 'frontend/keep.log', 'frontend/other.log', 'frontend/]x', 'frontend/ax'}
    assert 'frontend/.gitignore (pattern "dist-*")' in ignore.reason('frontend/dist-1/x')
    assert '.gitignore (pattern ".vite")' in ignore.reason('frontend/.vite/deps/a.js')
    # A directory named as a deliverable is judged as a directory.
    assert ignore.reason('build') and not ignore.reason('pkg/build')


def test_ignored_paths_are_not_copied_into_a_snapshot_nor_merged(tmp_path):
    project = tmp_path / 'project'
    write(project, {'.gitignore': '.vite\n*.cache\n', 'frontend/src/App.vue': 'one', 'frontend/.vite/deps/old.js': 'old',
                    'frontend/vite.config.js': 'export default {}', 'state.cache': 'local'})
    workspace = IsolatedWorkspace(project, 'a1')
    work = workspace.prepare()
    assert not (work / 'frontend/.vite').exists() and not (work / 'state.cache').exists()
    # The task's tools write caches and the task edits source.
    write(work, {'frontend/.vite/deps/chunk.js': 'cache', 'frontend/.vite/deps/_metadata.json': '{}', 'x.cache': 'c',
                 'frontend/src/App.vue': 'two', 'frontend/src/New.vue': 'new'})
    changes = workspace.plan()
    assert sorted(change['path'] for change in changes) == ['frontend/src/App.vue', 'frontend/src/New.vue']
    workspace.publish(changes)
    assert (project / 'frontend/src/App.vue').read_text() == 'two'
    assert (project / 'frontend/.vite/deps/old.js').read_text() == 'old' and not (project / 'x.cache').exists()
    assert sorted(path.name for path in (project / 'frontend/.vite/deps').iterdir()) == ['old.js']


def test_a_project_file_the_task_starts_ignoring_is_left_alone(tmp_path):
    project = tmp_path / 'project'
    write(project, {'.gitignore': '*.tmp\n', 'settings.json': '{"a": 1}', 'notes.md': 'n', 'gone.md': 'g'})
    workspace = IsolatedWorkspace(project, 'a2')
    work = workspace.prepare()
    # The task ignores two files it got from the project: it changes one and deletes the other.
    write(work, {'.gitignore': '*.tmp\nsettings.json\ngone.md\n', 'settings.json': '{"a": 2}'})
    (work / 'gone.md').unlink()
    changes = {change['path'] for change in workspace.plan()}
    assert changes == {'.gitignore', 'gone.md'}  # the deletion is the task's own act; the ignored edit stays local
    workspace.publish(workspace.plan())
    assert (project / 'settings.json').read_text() == '{"a": 1}' and not (project / 'gone.md').exists()


def test_removing_a_pattern_delivers_the_files(tmp_path):
    project = tmp_path / 'project'
    write(project, {'.gitignore': 'lib/\n', 'src/main.ts': 'm'})
    workspace = IsolatedWorkspace(project, 'a3')
    work = workspace.prepare()
    write(work, {'src/lib/api.ts': 'api'})
    assert workspace.plan() == []
    reason = unmerged_reason('src/lib/api.ts', Ignore(work))
    assert '.gitignore (pattern "lib/")' in reason and 'remove the pattern' in reason
    assert unmerged_reason('src/main.ts', Ignore(work)) is None
    write(work, {'.gitignore': '/lib/\n'})
    assert sorted(change['path'] for change in workspace.plan()) == ['.gitignore', 'src/lib/api.ts']


def test_a_linked_or_broken_gitignore_changes_nothing(tmp_path):
    project = tmp_path / 'project'
    write(project, {'a.txt': 'a', 'sub/b.txt': 'b', 'rules': '*.txt\n', 'sub/.gitignore': '[\n\\\n**\n!\n/\n'})
    (project / '.gitignore').symlink_to(project / 'rules')
    ignore = Ignore(project)
    assert not ignore.reason('a.txt') and ignore.reason('sub/b.txt')  # "**" alone ignores everything below
    (project / 'sub/.gitignore').write_bytes(b'\xff\xfe[\n')
    assert not Ignore(project).reason('sub/b.txt')


def test_a_refresh_keeps_the_task_copy_of_a_file_the_project_starts_ignoring(tmp_path):
    project = tmp_path / 'project'
    write(project, {'src/a.py': 'a', 'frontend/.vite/deps/x.js': 'cache', 'old.md': 'o'})
    workspace = IsolatedWorkspace(project, 'a4')
    work = workspace.prepare()
    assert (work / 'frontend/.vite/deps/x.js').is_file()  # nothing ignored it yet
    # A colleague's delivery adds the rule and removes a file.
    write(project, {'.gitignore': '.vite/\n', 'src/a.py': 'a2'})
    (project / 'old.md').unlink()
    workspace.refresh()
    assert (work / 'src/a.py').read_text() == 'a2' and not (work / 'old.md').exists()
    assert (work / 'frontend/.vite/deps/x.js').read_text() == 'cache' and not (workspace.base / 'frontend/.vite/deps/x.js').exists()
    assert workspace.plan() == []
