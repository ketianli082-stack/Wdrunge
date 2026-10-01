from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier

import pytest

from app.tools import _workspace
from app.tools.read_project_file import make_read_project_file
from app.tools.write_project_file import make_write_project_file


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(_workspace, 'WORKSPACE_ROOT', tmp_path)
    return tmp_path, make_read_project_file(('.',)), make_write_project_file(('.',))


def test_partial_edit_preserves_surroundings_newlines_mode_and_hash(files):
    root, read, write = files
    path = root / 'script.py'
    path.write_bytes(b'header\r\nfirst = 1\r\nsecond = 2\r\nfooter\r\n')
    path.chmod(0o755)
    revision = sha256(path.read_bytes()).hexdigest()
    assert revision in read.invoke({'path': 'script.py', 'include_hash': True})
    result = write.invoke({'path': 'script.py', 'expected_hash': revision, 'edits': [
        {'old_text': 'first = 1', 'new_text': 'first = 10'},
        {'old_text': 'second = 2', 'new_text': 'second = 20'},
    ]})
    assert 'Wrote' in result
    assert path.read_bytes() == b'header\r\nfirst = 10\r\nsecond = 20\r\nfooter\r\n'
    assert path.stat().st_mode & 0o777 == 0o755
    assert read.invoke({'path': 'script.py', 'offset': 8, 'limit': 10}).startswith('first = 10')


def test_failed_batch_and_stale_hash_never_write(files):
    root, read, write = files
    path = root / 'a.txt'
    path.write_text('one\ntwo\ntwo\n')
    before = path.read_bytes()
    for edits in ([{'old_text': 'one', 'new_text': 'new'}, {'old_text': 'two', 'new_text': 'X'}],
                  [{'old_text': 'missing', 'new_text': ''}],
                  [{'old_text': '', 'new_text': 'append'}]):
        with pytest.raises(ValueError):
            write.invoke({'path': 'a.txt', 'edits': edits})
        assert path.read_bytes() == before
    with pytest.raises(ValueError, match='changed'):
        write.invoke({'path': 'a.txt', 'content': 'stale', 'expected_hash': '0'*64})
    assert path.read_bytes() == before
    assert sorted(p.name for p in root.iterdir()) == ['a.txt']


def test_write_mode_validation_and_binary_files(files):
    root, _, write = files
    for payload in ({'path': 'new.txt'}, {'path': 'new.txt', 'content': 'a', 'edits': []},
                    {'path': 'new.txt', 'content': {}}, {'path': 'new.txt', 'edits': []}):
        with pytest.raises(ValueError):
            write.invoke(payload)
    with pytest.raises(ValueError, match='existing'):
        write.invoke({'path': 'new.txt', 'edits': [{'old_text': 'a', 'new_text': 'b'}]})
    assert not (root / 'new.txt').exists()
    (root / 'binary').write_bytes(b'\xff')
    with pytest.raises(UnicodeDecodeError):
        write.invoke({'path': 'binary', 'edits': [{'old_text': 'a', 'new_text': 'b'}]})
    assert (root / 'binary').read_bytes() == b'\xff'


def test_concurrent_edits_preserve_independent_changes_and_reject_stale_rewrite(files):
    root, _, write = files
    path = root / 'a.txt'
    path.write_text('one\ntwo\n')
    barrier = Barrier(2)
    def edit(old):
        barrier.wait()
        return write.invoke({'path': 'a.txt', 'edits': [{'old_text': old, 'new_text': old.upper()}]})
    with ThreadPoolExecutor(2) as executor:
        assert all('Wrote' in result for result in executor.map(edit, ['one', 'two']))
    assert path.read_text() == 'ONE\nTWO\n'
    revision = sha256(path.read_bytes()).hexdigest()
    def rewrite(content):
        barrier.wait()
        try:
            write.invoke({'path': 'a.txt', 'content': content, 'expected_hash': revision})
            return 'ok'
        except ValueError:
            return 'conflict'
    with ThreadPoolExecutor(2) as executor:
        assert sorted(executor.map(rewrite, ['first', 'second'])) == ['conflict', 'ok']


def test_partial_edit_cannot_bypass_managed_document_guard(files):
    from app.collaboration.tools import Context, active
    root, _, write = files
    (root / 'docs').mkdir()
    (root / 'docs/spec.md').write_text('original')
    token = active.set(Context(object(), 'p', 'product', 'c', 'ctx'))
    try:
        result = write.invoke({'path': 'docs/spec.md', 'edits': [{'old_text': 'original', 'new_text': 'bypass'}]})
        assert 'collaborate publish' in result
        assert (root / 'docs/spec.md').read_text() == 'original'
    finally:
        active.reset(token)
