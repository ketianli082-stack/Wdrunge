from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier

import pytest

from app.agents.tools import _workspace
from app.agents.tools.read_project_file import make_read_project_file
from app.agents.tools.write_project_file import make_write_project_file


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
        with pytest.raises(ValueError, match='collaboration_publish'):
            write.invoke({'path': 'docs/spec.md', 'edits': [{'old_text': 'original', 'new_text': 'bypass'}]})
        assert (root / 'docs/spec.md').read_text() == 'original'
    finally:
        active.reset(token)


def test_partial_reads_say_where_they_are_and_a_read_past_the_end_says_so(files):
    """Blog5: 77 of 198 reads were paged; the total length came only with include_hash,
    so members guessed offsets, and 3 reads past the end returned an empty string."""
    from app.agents.read_result import parse_read
    root, read, _ = files
    text = ''.join(f'line {i}\n' for i in range(1, 501))
    (root / 'long.txt').write_text(text)
    (root / 'short.txt').write_text('only\n')
    # A whole-file read is the body alone: nothing is added to the common case.
    assert read.invoke({'path': 'short.txt'}) == 'only\n' and read.invoke({'path': 'short.txt', 'force': True}) == 'only\n'
    head = read.invoke({'path': 'long.txt', 'limit': 100})
    assert head == text[:100] + f'\n[characters 0–100 of {len(text)}; next_offset=100]'
    tail = read.invoke({'path': 'long.txt', 'offset': 3000})
    assert tail == text[3000:] + f'\n[characters 3000–{len(text)} of {len(text)}]'
    assert read.invoke({'path': 'long.txt', 'offset': len(text) + 50}) == f'[offset {len(text) + 50} 处没有内容；文件共 {len(text)} 字符]'
    # The code index and search results give line numbers; the tool takes them directly.
    lines = read.invoke({'path': 'long.txt', 'start_line': 12, 'line_count': 3})
    start = text.index('line 12\n')
    assert lines == f'line 12\nline 13\nline 14\n\n[lines 12–14, characters {start}–{start + 24} of {len(text)}; next_offset={start + 24}]'
    assert read.invoke({'path': 'long.txt', 'start_line': 499}).endswith(f'[lines 499–500, characters {len(text) - 18}–{len(text)} of {len(text)}]')
    assert read.invoke({'path': 'short.txt', 'start_line': 1}) == 'only\n'
    assert '没有内容' in read.invoke({'path': 'long.txt', 'start_line': 900})
    for args in ({'path': 'long.txt', 'start_line': 2, 'offset': 5}, {'path': 'long.txt', 'line_count': 2}):
        with pytest.raises(ValueError):
            read.invoke(args)
    # One parser reads every form back, including results written before the position line.
    for result, offset in ((head, 0), (tail, 3000), (lines, 0), ('[sha256=ab; characters=9]\nabc\n[truncated; next_offset=3]', 0)):
        parsed = parse_read(result, offset)
        assert parsed.body and not parsed.body.endswith(']')
    parsed = parse_read(lines)
    assert (parsed.start, parsed.end, parsed.total, parsed.next_offset, parsed.lines) == (start, start + 24, len(text), start + 24, (12, 14))
    hashed = parse_read(read.invoke({'path': 'long.txt', 'offset': 10, 'limit': 20, 'include_hash': True}))
    assert hashed.sha == sha256(text.encode()).hexdigest() and hashed.body == text[10:30] and hashed.total == len(text)
    legacy = parse_read('abc\n[truncated; next_offset=103]', 100)
    assert (legacy.body, legacy.start, legacy.next_offset, legacy.whole) == ('abc', 100, 103, False)
