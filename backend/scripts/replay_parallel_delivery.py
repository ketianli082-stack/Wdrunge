"""Two module tasks of one role replayed as if they had started from the same project
state, with the platform's own IsolatedWorkspace: which files both changed, what the
second delivery meets today, and whether a file reconciled by hand can be delivered.

Defaults are the Blog11 frontend tasks (init, browse, publish). The first task's work is
its real files. The second task's work is its real files for everything only it touched;
frontend/src/tests/shell.spec.ts is rebuilt as init's file with the second task's edit,
because the real one was written on top of the first task's. HomeView.vue is left as init
had it: the second task only corrected a line the first one wrote.

    .venv/bin/python scripts/replay_parallel_delivery.py [--keep]

Prints the merged project's path with --keep, so its tests can be run there (link a
node_modules folder into frontend/ first).
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix='parallel-delivery-'))
os.environ['WDRUNGE_RUNS_DIR'] = str(ROOT / 'runs')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.collaboration.workspaces import IsolatedWorkspace, files  # noqa: E402
from app.storage import StorageConflictError  # noqa: E402

RUNS = Path.home() / 'Library/Application Support/Wdrunge Runs/Blog11-b1cc0df80701'
INIT, FIRST, SECOND = (RUNS / snapshot / 'work' / 'frontend' for snapshot in (
    '1b106b2a63fe4d7c8b44256787dd4b06', '4c1a495e86d6426590072aecfc303af9', '18ee0e7bbdcc4c19b6c6d5535654888d'))
SHARED_TEST = 'src/tests/shell.spec.ts'
SKIP = shutil.ignore_patterns('node_modules', 'dist', '.task-scratch', '.vite', 'coverage')


def state(root):
    return {name: path.read_bytes() for name, path in files(root).items()}


def overlay(target, before, after):
    """Apply to `target` what changed between two snapshots."""
    old, new = state(before), state(after)
    for name in sorted(old.keys() | new.keys()):
        if old.get(name) == new.get(name):
            continue
        path = target / name
        if name not in new:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(new[name])


def block(lines, start, end):
    return (next(i for i, line in enumerate(lines) if start in line), next(i for i, line in enumerate(lines) if end in line))


def main():
    project = ROOT / 'project'
    shutil.copytree(INIT, project / 'frontend', ignore=SKIP)
    first, second = IsolatedWorkspace(project, 'first'), IsolatedWorkspace(project, 'second')
    first.prepare()
    second.prepare()
    overlay(first.work / 'frontend', INIT, FIRST)
    overlay(second.work / 'frontend', FIRST, SECOND)
    shutil.copy(INIT / 'src/views/HomeView.vue', second.work / 'frontend/src/views/HomeView.vue')
    initial = (INIT / SHARED_TEST).read_text().splitlines(keepends=True)
    final = (SECOND / SHARED_TEST).read_text().splitlines(keepends=True)
    a, b = block(initial, '每个模块页面都能渲染占位内容', '未知路径渲染 404 页')
    x, y = block(final, '未验证时管理页只显示密码验证', '未知路径渲染 404 页')
    (second.work / 'frontend' / SHARED_TEST).write_text(''.join(initial[:a] + final[x:y] + initial[b:]))
    both = sorted(set(first.changed_paths()) & set(second.changed_paths()))
    print(f'first task changed {len(first.changed_paths())} files, second {len(second.changed_paths())}, both {both}')

    changes = first.plan()
    first.publish(changes)
    first.rebase(changes)
    shared = second.sync([item['path'] for item in changes])
    print(f"1. first delivers: merged {len(changes)} files; second snapshot pulled {len(shared['pulled'])}, kept {shared['kept']}")

    try:
        second.plan()
        print('2. second delivers: merged')
    except StorageConflictError as exc:
        print('2. second delivers:', exc)

    name = f'frontend/{SHARED_TEST}'
    reconciled = (SECOND / SHARED_TEST).read_bytes()  # both tasks' edits, as the real run ended
    (second.work / name).write_bytes(reconciled)
    try:
        second.plan()
        print('3. second delivers the file reconciled by hand: merged')
    except StorageConflictError as exc:
        print('3. second delivers the file reconciled by hand:', exc)

    second.rebase([{'path': name, 'content': (project / name).read_bytes()}])
    (second.work / name).write_bytes(reconciled)
    changes = second.plan()
    second.publish(changes)
    second.rebase(changes)
    print(f"4. the same delivery with the project's version as that file's base: merged {len(changes)} files")

    real = state(SECOND)
    merged = {name.removeprefix('frontend/'): data for name, data in state(project).items()}
    print('5. merged project against the real result, differing files:',
          sorted(name for name in real.keys() | merged.keys() if real.get(name) != merged.get(name)))
    if '--keep' in sys.argv:
        print('kept:', project)
    else:
        shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == '__main__':
    main()
