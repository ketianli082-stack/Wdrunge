"""Browser UI test with mocked API: no user records or credentials are touched."""
import json
from pathlib import Path
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import pytest


def test_permission_popup_once_always_and_revoke(tmp_path):
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright
    root = Path(__file__).resolve().parents[2]/'frontend/dist'
    if not (root/'index.html').exists():
        pytest.skip('Build frontend first')
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {'pending': [], 'grants': [], 'sandbox': {'available': True, 'backend': 'seatbelt', 'detail': '测试沙箱'},
             'settings': {'ai_review': True, 'whitelist': ['versions','git_status']}, 'reviewer': {'model':'qwen-flash','ready':True}}
    actions = []
    settings_changes = []
    request = {'id': 'approve-1', 'scope': '/example/project', 'reason': '需要人工确认', 'agent': 'testing', 'conversation': 'c',
               'operation': {'argv': ['npm', 'test'], 'cwd': '/example/project', 'network': False}}
    def api(route):
        path = route.request.url.split('/api/', 1)[1]
        data, status = {}, 200
        if path == 'execution/capabilities':
            data = {'pending': len(state['pending'])}
        elif path == 'execution/permissions':
            if route.request.headers.get('authorization') != 'Bearer test-only-token':
                status = 401
            data = state
        elif path.startswith('execution/permissions/'):
            actions.append(route.request.post_data_json['decision'])
            if actions[-1] == 'always':
                state['grants'] = [{**request, 'id': 'saved-grant'}]
            state['pending'] = []
        elif path == 'execution/grants/saved-grant/revoke':
            actions.append('revoke')
            state['grants'] = []
        elif path == 'execution/processes':
            data = {'processes': []}
        elif path == 'execution/settings':
            state['settings'] = route.request.post_data_json
            settings_changes.append(state['settings'])
            data = state['settings']
        elif path == 'projects':
            data = []
        elif path == 'team':
            data = {'agents': [], 'usage': {'agents': [], 'total_tokens': 0}}
        route.fulfill(status=status, content_type='application/json', body=json.dumps(data), headers={'Access-Control-Allow-Origin': '*'})
    try:
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                pytest.skip('Optional Chromium unavailable')
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            page.route('**/api/**', api)
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.get_by_role('button', name='执行权限', exact=True).click()
            page.get_by_label('本机授权码').fill('test-only-token')
            page.get_by_role('button', name='连接执行授权').click()
            page.get_by_text('自动审批与白名单', exact=True).click()
            page.get_by_role('checkbox', name='小模型自动审批', exact=False).uncheck()
            page.get_by_role('checkbox', name='小模型自动审批', exact=False).check()
            page.get_by_role('checkbox', name='版本查询免审批', exact=False).uncheck()
            assert settings_changes[-1] == {'ai_review':True,'whitelist':['git_status']}
            state['pending'] = [request]
            page.get_by_role('button', name='单次允许').wait_for()
            page.screenshot(path=str(tmp_path/'permission-popup.png'))
            page.get_by_role('button', name='单次允许').click()
            page.get_by_text('当前没有待确认操作。').wait_for()
            assert actions == ['once'] and not state['grants']
            state['pending'] = [{**request, 'id': 'approve-2'}]
            page.get_by_role('button', name='以后都允许').click()
            page.get_by_text('已保存的授权（1）').click()
            page.get_by_role('button', name='撤销授权').click()
            page.wait_for_function("!document.body.innerText.includes('已保存的授权（1）')")
            assert actions == ['once', 'always', 'revoke']
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
