"""Browser UI test for service connections, with a mocked API: no real records touched."""
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


# WebKit is what the desktop app renders with; it does not focus clicked buttons.
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_team_menu_opens_connections_create_import_and_test(tmp_path, engine):
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright
    root = Path(__file__).resolve().parents[2] / 'frontend/dist'
    if not (root / 'index.html').exists():
        pytest.skip('Build frontend first')

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    project = {'id': 'p1', 'name': '个人博客', 'workspace_path': '/example/blog', 'created_at': '2026-10-04T00:00:00Z',
               'updated_at': '2026-10-04T00:00:00Z', 'conversations': []}
    types = [{'type': 'mysql', 'label': 'MySQL', 'port': 3306, 'can_provision': True},
             {'type': 'redis', 'label': 'Redis', 'port': 6379, 'can_provision': False}]
    connections, created, imported = [], [], []

    def public(body, identifier, source=None):
        return {'id': identifier, 'name': body['name'], 'label': body.get('label') or body['type'], 'type': body['type'],
                'type_label': body['type'], 'host': body['host'], 'port': body.get('port'), 'username': body.get('username', ''),
                'database': body.get('database', ''), 'scheme': '', 'env_prefix': body['name'].upper().replace('-', '_'),
                'provision': body.get('provision', False), 'can_provision': body['type'] == 'mysql', 'projects': body.get('projects'),
                'extra': [{**e, 'value': '' if e['secret'] else e['value'], 'has_value': True} for e in body.get('extra', [])],
                'note': '', 'has_password': bool(body.get('password')), 'source': source or {'kind': 'manual'}}

    def api(route):
        path = route.request.url.split('/api/', 1)[1].split('?')[0]
        method = route.request.method
        data, status = {}, 200
        if path.startswith('resources') and route.request.headers.get('authorization') != 'Bearer test-only-token':
            status, data = 401, {'detail': 'unauthorized'}
        elif path == 'resources' and method == 'GET':
            data = {'connections': connections, 'types': types}
        elif path == 'resources' and method == 'POST':
            body = route.request.post_data_json
            created.append(body)
            connections.append(public(body, f'c{len(connections) + 1}'))
            status, data = 201, connections[-1]
        elif path == 'resources/test':
            data = {'ok': True, 'detail': '已连接，服务器版本 8.4.8'}
        elif path == 'resources/docker':
            data = {'containers': [{'container_id': 'abc', 'container': 'mysql8', 'image': 'mysql:8.4.8', 'type': 'mysql',
                                    'type_label': 'MySQL', 'port': 3306, 'username': 'root', 'has_password': True,
                                    'imported': False, 'known_type': True}]}
        elif path == 'resources/docker/import':
            imported.append(route.request.post_data_json)
            connections.append(public({'name': 'mysql', 'type': 'mysql', 'host': '127.0.0.1', 'port': 3306, 'password': 'x',
                                       'provision': True, 'label': 'MySQL（Docker：mysql8）'}, 'c9', {'kind': 'docker', 'container': 'mysql8'}))
            status, data = 201, {**connections[-1], 'test': {'ok': False, 'detail': '账号或密码不正确。容器启动参数里的密码可能已被修改过'}}
        elif path == 'resources/projects/p1':
            data = {'project_id': 'p1', 'data_dir': '/example/blog/.wdrunge-data', 'database': 'sqlite', 'resources': []}
        elif path == 'execution/capabilities':
            data = {'pending': 0}
        elif path == 'execution/permissions':
            status = 401
        elif path == 'projects':
            data = [project]
        elif path == 'team':
            data = {'agents': [], 'usage': {'agents': [], 'total_tokens': 0}}
        route.fulfill(status=status, content_type='application/json', body=json.dumps(data),
                      headers={'Access-Control-Allow-Origin': '*'})
    try:
        with sync_playwright() as playwright:
            browser_type = getattr(playwright, engine)
            if not Path(browser_type.executable_path).is_file():
                pytest.skip(f'Optional {engine} unavailable')
            browser = browser_type.launch()
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            page.route('**/api/**', api)
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.get_by_role('button', name='Agent 团队').click()
            page.get_by_role('menuitem', name='服务连接与数据库').click()
            page = page.locator('.resource-page')
            page.get_by_label('本机授权码').fill('test-only-token')
            page.get_by_role('button', name='连接', exact=True).click()
            page.get_by_text('还没有连接').wait_for()

            page.get_by_role('button', name='＋ 新建连接').click()
            page.get_by_label('密码', exact=True).fill('root-password')
            page.get_by_role('button', name='＋ 添加字段').click()
            page.get_by_label('变量名', exact=True).fill('mysql_ssl_mode')
            page.get_by_label('值', exact=True).fill('DISABLED')
            page.get_by_text('MYSQL_SSL_MODE').first.wait_for()
            page.get_by_role('button', name='测试连接').click()
            page.get_by_text('已连接，服务器版本 8.4.8').wait_for()
            page.page.screenshot(path=str(tmp_path / 'resource-form.png'))
            page.get_by_role('button', name='保存').click()
            page.get_by_text('每个项目自动创建独立数据库和账号').wait_for()
            assert created == [{'name': 'mysql', 'label': '', 'type': 'mysql', 'host': '127.0.0.1', 'port': 3306, 'username': 'root',
                                'password': 'root-password', 'clear_password': False, 'database': '', 'scheme': '', 'env_prefix': '',
                                'provision': True, 'projects': None, 'note': '',
                                'extra': [{'key': 'MYSQL_SSL_MODE', 'value': 'DISABLED', 'secret': False}]}]

            page.get_by_role('button', name='从 Docker 导入').click()
            page.get_by_role('button', name='导入', exact=True).click()
            page.get_by_text('容器启动参数里的密码可能已被修改过').wait_for()
            page.get_by_text('已导入').wait_for()
            assert imported == [{'container': 'mysql8'}]
            page.page.screenshot(path=str(tmp_path / 'resource-list.png'))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
