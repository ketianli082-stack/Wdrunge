"""Real OS smoke test. Run on macOS/Linux hosts with the sandbox backend installed."""
import asyncio
import json
import os
from pathlib import Path
import sys
import pytest
from app.execution import sandbox


def test_native_sandbox_enforces_write_and_secret_boundaries(tmp_path):
    if not sandbox.capability()['available']:
        pytest.skip('Native sandbox backend not installed')
    root = tmp_path/'project'
    root.mkdir()
    scratch = tmp_path/'scratch'
    scratch.mkdir()
    outside = tmp_path/'outside.txt'
    secret = root/'.env'
    secret.write_text('PRIVATE')
    code = f'''from pathlib import Path
Path({str(root/'ok.txt')!r}).write_text('ok')
for path, action in [({str(outside)!r}, 'write'), ({str(secret)!r}, 'read')]:
    try:
        Path(path).write_text('escape') if action == 'write' else Path(path).read_text()
    except (PermissionError, OSError):
        print('blocked', action)
    else:
        raise RuntimeError('Sandbox allowed ' + action)
print('sandbox-ok')
'''
    command = sandbox.command_line([sys.executable, '-c', code], root, root, scratch)
    async def run():
        process = await asyncio.create_subprocess_exec(*command, cwd=root, env=sandbox.clean_environment(scratch, root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(process.communicate(), 15)
        return process.returncode, output.decode()
    status, output = asyncio.run(run())
    assert status == 0 and 'sandbox-ok' in output, output
    assert (root/'ok.txt').read_text() == 'ok' and not outside.exists()


def test_guarded_names_are_blocked_in_the_project_but_not_in_dependencies(tmp_path):
    """Blog1/Blog2: the .git/.agents rule matched at any depth, so FastAPI 0.142 (a wheel
    with a .agents/ directory) installed as an empty package and uv could not create its
    cache (sdists-v9/.git). The rule now guards the project tree, not installed packages."""
    if not sandbox.capability()['available'] or sandbox.capability()['backend'] != 'seatbelt':
        pytest.skip('Seatbelt sandbox not available')
    root = tmp_path/'My Project (1)'
    root.mkdir()
    scratch = tmp_path/'scratch'
    scratch.mkdir()
    allowed = ['.venv/lib/python3.13/site-packages/fastapi/.agents/skills/x.md', 'frontend/node_modules/pkg/.git/HEAD',
               '.cache/uv/sdists-v9/.git/x']
    denied = ['.git/hooks/pre-commit', 'server/.git/config', '.agents/skills/x.md', 'docs/.codex/x', '.ssh/id']
    code = f'''from pathlib import Path
root, scratch = Path({str(root)!r}), Path({str(scratch)!r})
for name in {allowed!r}:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text('ok')
(scratch / 'uv-cache/sdists-v9/.git').mkdir(parents=True)
for name in {denied!r}:
    try:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text('escape')
    except (PermissionError, OSError):
        print('blocked', name)
    else:
        raise RuntimeError('Sandbox allowed ' + name)
print('sandbox-ok')
'''
    command = sandbox.command_line([sys.executable, '-c', code], root, root, scratch)
    async def run():
        process = await asyncio.create_subprocess_exec(*command, cwd=root, env=sandbox.clean_environment(scratch, root),
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(process.communicate(), 15)
        return process.returncode, output.decode()
    status, output = asyncio.run(run())
    assert status == 0 and 'sandbox-ok' in output, output
    assert all((root / name).read_text() == 'ok' for name in allowed)
    assert output.count('blocked') == len(denied)


def test_native_browser_interactions_errors_and_screenshot(tmp_path, monkeypatch):
    pytest.importorskip('playwright')
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from types import SimpleNamespace
    from playwright.sync_api import sync_playwright
    from app.execution import permissions
    from app.execution.processes import processes
    from app.agents.tools.browser_verify import browser_verify
    from app.agents.tools._workspace import active_workspace
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip('Optional Chromium installation unavailable')
    class Approve:
        store = SimpleNamespace(sqlite_path=None)
        async def authorize(self, *args, **kwargs):
            pass
    monkeypatch.setattr(permissions, 'current', Approve())
    monkeypatch.setattr(processes, 'jobs', {})
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'<title>Verification</title><button id="go" onclick="document.querySelector(\'#result\').textContent=\'Done\'">Go</button><p id="result">Ready</p>')
            if self.path == '/error':
                self.wfile.write(b'<script>console.error("console evidence");throw Error("page evidence")</script>')
            if self.path == '/tall':
                self.wfile.write(b'<div style="height:13000px;background:linear-gradient(red,blue)"></div>')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def scenario():
        token = active_workspace.set(tmp_path)
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            good = await browser_verify.ainvoke({'url': url, 'screenshot': True, 'actions': [
                {'action': 'click', 'selector': '#go'},
                {'action': 'assert_text', 'selector': '#result', 'value': 'Done'}]})
            assert good.get('passed'), good.get('error', good)
            assert (tmp_path / good['screenshot']).read_bytes().startswith(b'\x89PNG')
            # Named for the page and short enough to copy into a report without a slip.
            import re
            assert re.fullmatch(r'docs/testing/shot-home-[0-9a-f]{4}\.png', good['screenshot']), good['screenshot']
            # A developer's check keeps no picture unless it asks for one.
            smoke = await browser_verify.ainvoke({'url': url})
            assert smoke['passed'] and 'screenshot' not in smoke and 'Ready' in smoke['text']
            assert len(list((tmp_path / 'docs' / 'testing').glob('*.png'))) == 1
            bad = await browser_verify.ainvoke({'url': url+'/error'})
            assert not bad['passed'] and bad['page_errors'], bad
            assert any('console evidence' in entry['text'] for entry in bad['console'])
            # A long page comes back as full-width tiles, attached to the result for the model.
            tall = await browser_verify.ainvoke({'url': url+'/tall', 'screenshot': True})
            assert tall['passed'] and len(tall['screenshots']) == 3 and tall['screenshot'] == tall['screenshots'][0], tall
            assert tall['screenshot_covers'].startswith('top 12000px of'), tall
            from langchain_core.messages import ToolMessage
            from app.agents.page_images import with_page_images
            result = ToolMessage(content=json.dumps(tall), name='browser_verify', tool_call_id='c1')
            shown, tokens = with_page_images([result])
            assert [part['type'] for part in shown[0].content] == ['text', 'text', 'image_url', 'image_url', 'image_url']
            assert 3 * 2000 < tokens <= 3 * 2570
        finally:
            await processes.close()
            active_workspace.reset(token)
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>博客</title></head><body><div id="app"><main class="page">
<p class="meta">分类：<br><span class="chip">前端</span></p><p id="count">加载中</p>
<form class="search"><input name="q" type="search" placeholder="搜索文章"><input name="pw" type="password" value="hunter2-secret"><button type="submit" class="btn">搜索</button></form>
<div id="cards"></div>
<nav class="pagination"><a href="/?page=1" class="page-btn">1</a><a href="/?page=2" class="page-btn">2</a><a href="/?page=2" class="page-btn">下一页</a></nav>
<div class="modal" style="display:none"><button class="confirm">确认</button></div>
<p id="result">Ready</p><button id="go" onclick="document.querySelector('#result').textContent='Done'">Go</button></main></div>
<script>
for (let i = 1; i <= 10; i++) document.querySelector('#cards').insertAdjacentHTML('beforeend',
  `<article class="card"><h2 class="card__title"><a href="/post/${i}">第 ${i} 篇</a></h2><p class="card__summary">摘要 ${i}</p></article>`);
setTimeout(() => { document.querySelector('#count').textContent = '找到 10 篇 · 第 1 / 1 页'; }, 1200);
</script></body></html>""".encode()


def test_native_browser_selectors_text_and_failure_report(tmp_path, monkeypatch):
    """Blog4 and Blog10: 58 checks failed because a selector matched several elements,
    12 on a line break inside the expected text, and 46 timeouts returned nothing about
    the page, so the next selector was a guess."""
    pytest.importorskip('playwright')
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from types import SimpleNamespace
    from playwright.sync_api import sync_playwright
    from app.execution import permissions
    from app.execution.processes import processes
    from app.agents.tools.browser_verify import browser_verify
    from app.agents.tools._workspace import active_workspace
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip('Optional Chromium installation unavailable')
    class Approve:
        store = SimpleNamespace(sqlite_path=None)
        async def authorize(self, *args, **kwargs):
            pass
    monkeypatch.setattr(permissions, 'current', Approve())
    monkeypatch.setattr(processes, 'jobs', {})
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(PAGE)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def scenario():
        token = active_workspace.set(tmp_path)
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            async def check(*actions):
                return await browser_verify.ainvoke({'url': url, 'screenshot': True, 'actions': [
                    {'action': action, 'selector': selector, 'value': value} for action, selector, value in actions]})
            good = await check(('wait_visible', '.card', ''),               # ten cards: any visible one serves
                               ('assert_text', '.card__summary', '摘要 7'),   # the text is in one of the ten
                               ('assert_text', '#count', '找到 10 篇'),       # arrives after the page loads
                               ('assert_text', '.meta', '分类：前端'),         # a line break sits inside it
                               ('click', '.page-btn >> nth=0', ''), ('click', '#go', ''), ('assert_text', '#result', 'Done'))
            assert good.get('passed'), good.get('error', good)
            assert [action.get('matches') for action in good['actions'][:3]] == [10, 10, None]
            assert good['actions'][3]['note'] == 'matched ignoring spaces and line breaks'

            several = await check(('click', ".pagination a[href='/?page=2']", ''))
            details = several['error_details']
            assert (details['category'], details['code']) == ('invalid_arguments', 'ambiguous_selector'), several
            assert 'matches 2' in several['error'] and '0) <a href="/?page=2" class="page-btn"> "2"' in several['error']
            assert '1) <a href="/?page=2" class="page-btn"> "下一页"' in several['error'] and '>> nth=0' in details['next_action']

            missing = await check(('wait_visible', '.card__heading', ''))
            details = missing['error_details']
            assert (details['code'], details['selector_matches']) == ('selector_not_found', 0), missing
            assert 'article.card' in missing['dom'] and '… 9 more article.card built like the one above' in missing['dom']
            assert 'h2.card__title > a[href="/post/1"] "第 1 篇"' in missing['dom'] and 'div.modal (hidden)' in missing['dom']
            assert 'input[name="q"][type="search"][placeholder="搜索文章"]' in missing['dom'] and 'input[name="pw"][type="password"]' in missing['dom']
            assert 'hunter2' not in json.dumps(missing) and '摘要 3' in missing['text'] and missing['screenshot']

            hidden = await check(('wait_visible', '.modal .confirm', ''))
            assert (hidden['error_details']['code'], hidden['error_details']['selector_matches']) == ('action_timeout', 1), hidden

            wrong = await check(('assert_text', '.page-btn', '上一页'))
            assert wrong['error_details']['code'] == 'assertion_failed', wrong
            assert "got 0) '1'; 1) '2'; 2) '下一页' (3 elements match the selector)" in wrong['error'] and wrong['dom']

            syntax = await check(('click', '#cards article:first-child text=第 1 篇', ''))
            assert (syntax['error_details']['category'], syntax['error_details']['code']) == ('invalid_arguments', 'invalid_selector'), syntax
            assert ':has-text(' in syntax['error_details']['next_action']
        finally:
            await processes.close()
            active_workspace.reset(token)
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_command_tool_graph_foreground_and_background(tmp_path, monkeypatch):
    """Real public tool -> ToolNode -> process manager -> native sandbox."""
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage
    from langgraph.graph import StateGraph, MessagesState, START, END
    from langgraph.prebuilt import ToolNode
    from app.execution import permissions
    from app.execution.processes import processes
    from app.agents.tools.development import development_tools
    from app.agents.tools.validation import tool_error
    from app.agents.tools._workspace import active_workspace, source_workspace
    import json
    if not sandbox.capability()['available']:
        pytest.skip('Native sandbox backend unavailable')
    reviewed=[]
    class Approve:
        store=SimpleNamespace(sqlite_path=None)
        async def authorize(self, operation, **kwargs):reviewed.append(operation)
    monkeypatch.setattr(permissions,'current',Approve())
    monkeypatch.setattr(processes,'jobs',{})
    builder=StateGraph(MessagesState)
    builder.add_node('tools',ToolNode(list(development_tools),handle_tool_errors=tool_error))
    builder.add_edge(START,'tools');builder.add_edge('tools',END)
    graph=builder.compile()
    async def call(name,arguments):
        result=await graph.ainvoke({'messages':[AIMessage(content='',tool_calls=[{'id':'call','name':name,'args':arguments}])]})
        reply=result['messages'][-1]
        assert reply.status=='success',reply.content
        data=json.loads(reply.content)
        assert 'error' not in data,data
        return data
    async def run():
        token=active_workspace.set(tmp_path)
        source_token=source_workspace.set(tmp_path)
        try:
            result=await call('execute_command',{'argv': [sys.executable, '-c','print("dispatch-ok")']})
            assert result['exit_code']==0 and 'dispatch-ok' in result['output']
            jobs=[]
            for name in ('frontend','backend'):
                jobs.append(await call('execute_command',{'argv': [sys.executable, '-u','-c','import time;print("ready");time.sleep(30)'],'background':True,'name':name,'timeout':40}))
                assert jobs[-1]['name'] == name
            assert len({job['id'] for job in jobs})==2
            listed=await call('manage_process',{'action':'list'})
            assert len(listed['processes'])==2 and listed['finished_commands']==1
            for job in jobs:
                assert (await call('manage_process',{'action':'status','process_id':job['id']}))['state']=='running'
                assert (await call('manage_process',{'action':'stop','process_id':job['id']}))['state']=='exited'
            assert len(reviewed)==3
        finally:
            await processes.close(workspace=tmp_path)
            source_workspace.reset(source_token)
            active_workspace.reset(token)
    asyncio.run(run())


def test_native_browser_forwards_allowed_websocket_and_blocks_other_origins(tmp_path, monkeypatch):
    """Exercise the WebSocket callback used by Vite HMR, through the real runner."""
    pytest.importorskip('playwright')
    import base64
    import hashlib
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace
    from playwright.sync_api import sync_playwright
    from app.execution import permissions
    from app.execution.processes import processes
    from app.agents.tools.browser_verify import browser_verify
    from app.agents.tools._workspace import active_workspace

    if not sandbox.capability()['available']:
        pytest.skip('Native sandbox backend unavailable')
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip('Optional Chromium installation unavailable')

    class Approve:
        store = SimpleNamespace(sqlite_path=None)
        async def authorize(self, *args, **kwargs):
            pass
    monkeypatch.setattr(permissions, 'current', Approve())
    monkeypatch.setattr(processes, 'jobs', {})

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/hmr' and self.headers.get('Upgrade', '').lower() == 'websocket':
                key = self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
                self.send_response(101)
                self.send_header('Upgrade', 'websocket')
                self.send_header('Connection', 'Upgrade')
                self.send_header('Sec-WebSocket-Accept', base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
                self.end_headers()
                self.wfile.write(b'\x81\x09hmr-ready')
                self.wfile.flush()
                # Keep the connection open until the disposable browser closes.
                self.rfile.read(2)
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            host = 'localhost' if self.path == '/blocked' else '127.0.0.1'
            self.wfile.write((f'<p id="hmr">waiting</p><script>const ws=new WebSocket("ws://{host}:{self.server.server_port}/hmr");'
                'ws.onmessage=e=>{const p=document.querySelector("#hmr");p.textContent=e.data;p.dataset.ready="true";};'
                'ws.onclose=()=>{const p=document.querySelector("#hmr");p.textContent="closed";p.dataset.closed="true";};</script>').encode())
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario():
        token = active_workspace.set(tmp_path)
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            good = await browser_verify.ainvoke({'url': url, 'screenshot': True, 'actions': [
                {'action': 'wait_visible', 'selector': '[data-ready="true"]'},
                {'action': 'assert_text', 'selector': '#hmr', 'value': 'hmr-ready'}]})
            assert good.get('passed'), good
            assert (tmp_path / good['screenshot']).read_bytes().startswith(b'\x89PNG')
            bad = await browser_verify.ainvoke({'url': url+'/blocked', 'actions': [
                {'action': 'wait_visible', 'selector': '[data-closed="true"]'},
                {'action': 'assert_text', 'selector': '#hmr', 'value': 'closed'}]})
            assert not bad['passed'], bad
            assert any('Blocked unapproved WebSocket origin' in error for error in bad['page_errors']), bad
        finally:
            await processes.close(workspace=tmp_path)
            active_workspace.reset(token)
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


SERVER_THAT_CRASHES = '''
import asyncio, os, sys
from types import SimpleNamespace
from pathlib import Path
from app.execution import permissions
from app.execution.processes import processes
from app.agents.tools._workspace import active_workspace, source_workspace

class Approve:
    store = SimpleNamespace(sqlite_path=None)
    async def authorize(self, *args, **kwargs):
        pass

async def main():
    permissions.current = Approve()
    root = Path(sys.argv[1])
    active_workspace.set(root); source_workspace.set(root)
    job = await processes.start([sys.executable, '-c', 'import os, time; print(os.getpid(), flush=True); time.sleep(60)'],
                                '.', 120, 'svc', keep=True)
    while not b''.join(job.logs).strip():
        await asyncio.sleep(.05)
    print(b''.join(job.logs).decode().strip(), flush=True)
    os._exit(1)  # crash: no shutdown hook, no cleanup

asyncio.run(main())
'''


def test_commands_die_with_a_crashed_backend(tmp_path):
    """A kept service must not outlive the backend that started it, even after SIGKILL-like exits."""
    import subprocess, time
    if not sandbox.capability()['available']:
        pytest.skip('Native sandbox backend not installed')
    root = tmp_path / 'project'
    root.mkdir()
    backend = Path(__file__).resolve().parents[1]
    crashed = subprocess.run([sys.executable, '-c', SERVER_THAT_CRASHES, str(root)], cwd=backend, capture_output=True,
                             text=True, timeout=60, env={**os.environ, 'PYTHONPATH': str(backend)})
    pid = int(crashed.stdout.split()[-1])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.05)
    else:
        os.kill(pid, 9)
        pytest.fail(f'command {pid} kept running after its backend exited')


def test_supervised_commands_report_their_own_exit_status(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.execution import permissions
    from app.execution.processes import processes
    from app.agents.tools._workspace import active_workspace, source_workspace
    if not sandbox.capability()['available']:
        pytest.skip('Native sandbox backend not installed')
    class Approve:
        store = SimpleNamespace(sqlite_path=None)
        async def authorize(self, *args, **kwargs):
            pass
    monkeypatch.setattr(permissions, 'current', Approve())
    monkeypatch.setattr(processes, 'jobs', {})
    async def run():
        tokens = active_workspace.set(tmp_path), source_workspace.set(tmp_path)
        try:
            exited = await processes.run([sys.executable, '-c', 'raise SystemExit(3)'], '.', 30)
            killed = await processes.run([sys.executable, '-c', 'import os, signal; os.kill(os.getpid(), signal.SIGTERM)'], '.', 30)
            return exited['exit_code'], killed['exit_code']
        finally:
            await processes.close()
            source_workspace.reset(tokens[1]); active_workspace.reset(tokens[0])
    assert asyncio.run(run()) == (3, -15)


def test_commands_get_room_for_bundlers_and_long_services(tmp_path, monkeypatch):
    """Blog10: with 256 open files Vite could not bundle a UI library and reported
    "Rollup failed to resolve import"; 600 CPU seconds would end a long-lived
    development server. A command may hold well over a thousand files open at once."""
    from types import SimpleNamespace
    from app.execution import permissions
    from app.execution.processes import processes
    from app.agents.tools._workspace import active_workspace, source_workspace
    if not sandbox.capability()['available']:
        pytest.skip('Native sandbox backend not installed')
    class Approve:
        store = SimpleNamespace(sqlite_path=None)
        async def authorize(self, *args, **kwargs):
            pass
    monkeypatch.setattr(permissions, 'current', Approve())
    monkeypatch.setattr(processes, 'jobs', {})
    code = '''import resource
held = [open(f'f{index}.txt', 'w') for index in range(1500)]
print(resource.getrlimit(resource.RLIMIT_NOFILE)[0], resource.getrlimit(resource.RLIMIT_CPU)[0], len(held))
'''
    async def run():
        tokens = active_workspace.set(tmp_path), source_workspace.set(tmp_path)
        try:
            return await processes.run([sys.executable, '-c', code], '.', 30)
        finally:
            await processes.close()
            source_workspace.reset(tokens[1]); active_workspace.reset(tokens[0])
    result = asyncio.run(run())
    assert result['exit_code'] == 0, result['output']
    files, cpu, held = map(int, result['output'].split())
    assert files >= 4096 and cpu >= 4 * 3600 and held == 1500


def test_a_command_without_network_fails_fast_and_says_why(tmp_path, monkeypatch):
    """Blog11: three npm commands run with network=false hung to their timeouts (180,
    300 and 120 s) with no output, because npm retries a refused request for 70 s, and
    the timeout advice (inspect the timeout) was answered with a longer timeout. A
    refused lookup now ends the command at once and the result names network=false."""
    import shutil
    import time
    from types import SimpleNamespace
    from app.execution import permissions
    from app.execution.processes import processes
    from app.agents.tools import development
    from app.agents.tools._workspace import active_workspace, source_workspace
    if not sandbox.capability()['available']:
        pytest.skip('Native sandbox backend not installed')
    class Approve:
        store = SimpleNamespace(sqlite_path=None)
        async def authorize(self, *args, **kwargs):
            pass
    monkeypatch.setattr(permissions, 'current', Approve())
    monkeypatch.setattr(processes, 'jobs', {})
    lookup = [sys.executable, '-c', 'import socket; socket.getaddrinfo("registry.npmjs.org", 443)']
    environment = [sys.executable, '-c', 'import os; print(os.environ.get("npm_config_fetch_retries"))']

    async def run():
        tokens = active_workspace.set(tmp_path), source_workspace.set(tmp_path)
        try:
            refused = await development.execute_command.ainvoke({'argv': lookup})
            assert refused['exit_code'] == 1 and refused['network'] is False
            assert refused['warning'] == development.LOOKUP_WARNING, refused
            # Expected to fail, so it counts as a success: the warning is still there.
            expected = await development.execute_command.ainvoke({'argv': lookup, 'ok_exit_codes': [1]})
            assert 'error' not in expected and expected['warning'] == development.LOOKUP_WARNING
            silent = await development.execute_command.ainvoke({'argv': [sys.executable, '-c', 'import time; time.sleep(30)'], 'timeout': 1})
            assert silent['timed_out'] and silent['error_details']['next_action'] == development.TIMEOUT_WITHOUT_NETWORK
            online = await development.execute_command.ainvoke({'argv': [sys.executable, '-c', 'import time; time.sleep(30)'],
                                                                'timeout': 1, 'network': True})
            assert online['timed_out'] and 'network=false' not in online['error_details']['next_action'] and 'warning' not in online
            assert (await development.execute_command.ainvoke({'argv': environment}))['output'].strip() == '0'
            assert (await development.execute_command.ainvoke({'argv': environment, 'network': True}))['output'].strip() == 'None'
            if shutil.which('npm'):
                started = time.monotonic()
                npm = await development.execute_command.ainvoke({'argv': ['npm', 'view', 'left-pad', 'version'], 'timeout': 60})
                assert time.monotonic() - started < 30 and not npm.get('timed_out'), npm
                assert npm['exit_code'] == 1 and npm['warning'] == development.LOOKUP_WARNING, npm
        finally:
            await processes.close()
            source_workspace.reset(tokens[1]); active_workspace.reset(tokens[0])
    asyncio.run(run())
