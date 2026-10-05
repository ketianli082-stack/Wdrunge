"""Real OS smoke test. Run on macOS/Linux hosts with the sandbox backend installed."""
import asyncio
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
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def scenario():
        token = active_workspace.set(tmp_path)
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            good = await browser_verify.ainvoke({'url': url, 'actions': [
                {'action': 'click', 'selector': '#go'},
                {'action': 'assert_text', 'selector': '#result', 'value': 'Done'}]})
            assert good.get('passed'), good.get('error', good)
            assert Path(good['screenshot']).read_bytes().startswith(b'\x89PNG')
            bad = await browser_verify.ainvoke({'url': url+'/error'})
            assert not bad['passed'] and bad['page_errors'], bad
            assert any('console evidence' in entry['text'] for entry in bad['console'])
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
            result=await call('execute_command',{'command':sys.executable,'arguments':['-c','print("dispatch-ok")']})
            assert result['exit_code']==0 and 'dispatch-ok' in result['output']
            jobs=[]
            for name in ('frontend','backend'):
                jobs.append(await call('execute_command',{'command':sys.executable,'arguments':['-u','-c','import time;print("ready");time.sleep(30)'],'background':True,'name':name,'timeout':40}))
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
            good = await browser_verify.ainvoke({'url': url, 'actions': [
                {'action': 'wait_visible', 'selector': '[data-ready="true"]'},
                {'action': 'assert_text', 'selector': '#hmr', 'value': 'hmr-ready'}]})
            assert good.get('passed'), good
            assert Path(good['screenshot']).read_bytes().startswith(b'\x89PNG')
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
