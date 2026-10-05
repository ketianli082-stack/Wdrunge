"""Exercise the built environment panel and task journal view against a mocked local API."""
import json
from pathlib import Path
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest


def test_environment_panel_and_task_journal(tmp_path):
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright
    root = Path(__file__).resolve().parents[2] / 'frontend/dist'
    if not (root / 'index.html').exists():
        pytest.skip('Build frontend first')
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    conversation = dict(id='c', project_id='p', title='Env test', status='idle', project_name='Test project', workspace_path='/tmp/test',
                        messages=[], activities=[], created_at='2026-10-01T00:00:00Z', updated_at='2026-10-01T00:00:00Z')
    task = dict(id='leader:t', conversation='c', summary='实现接口', sender='leader', recipient='backend', state='running',
                goal='实现接口', result='', dependencies=[], delivered=[])
    journal = dict(request='leader:t', attempts=[dict(attempt=1, mode='fresh', outcome='interrupted', reason='StreamChunkTimeoutError'),
                                                 dict(attempt=2, mode='continue')],
                   last_failure=dict(tool='execute_command', code='connection_failed', error='curl exited 7'),
                   notes=[dict(kind='done', text='backend/app.py 已实现')], facts='Files: backend/app.py wrote')
    state = dict(document=[], request=[task], message=[], goal=[], settings=[], journal=[journal])
    environment = dict(host=dict(os='macOS', userland='BSD'), reserved_ports=[17420, 1420],
                       services=[dict(name='api', kind='http', port=8100, host='127.0.0.1', status='verified', source='agent',
                                      claimed=True, owner='backend', command=dict(argv=['python3', 'app.py'], cwd='backend'),
                                      runtime=dict(state='running', process_id='p1', kept=True, started_by='backend'))],
                       commands=[dict(name='test', argv=['pytest', '-q'], cwd='backend', status='verified')])
    actions = []
    def api(route):
        path = route.request.url.split('/api/', 1)[1].split('?', 1)[0]; data = {}
        if route.request.method == 'OPTIONS':
            route.fulfill(status=204, headers={'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*'}); return
        if path == 'projects': data = [dict(id='p', name='Test project', workspace_path='/tmp/test', conversations=[conversation])]
        elif path == 'conversations/c': data = conversation
        elif path == 'team': data = dict(agents=[], usage=dict(agents=[], total_tokens=0))
        elif path == 'projects/p/collaboration': data = state
        elif path == 'projects/p/collaboration/commands':
            body = route.request.post_data_json; actions.append(body)
            if body['action'] == 'environment':
                data = environment
            elif body['action'] == 'register_service':
                service = dict(name=body['payload']['name'], kind='http', port=body['payload'].get('port', 8101), status='declared',
                               source='user', claimed=True)
                environment['services'].append(service); data = service
        elif path == 'projects/p/collaboration/services/api/stop':
            actions.append(dict(action='stop_service', payload='api'))
            environment['services'][0]['runtime'] = dict(state='stopped'); data = dict(stopped='api', process_id='p1')
        elif path.endswith('/files'): data = dict(files=[])
        elif path.endswith('/events'): data = dict(events=[], cursor=0, has_more=False)
        elif path == 'execution/capabilities': data = dict(pending=0)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(data), headers={'Access-Control-Allow-Origin': '*'})
    try:
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file(): pytest.skip('Optional Chromium unavailable')
            browser = playwright.chromium.launch(); page = browser.new_page(viewport=dict(width=1440, height=1000))
            errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
            page.route('**/api/**', api); page.goto(f'http://127.0.0.1:{server.server_port}')
            page.get_by_role('button', name='任务与协作', exact=True).click()
            page.get_by_text('工作记录（2 次执行）', exact=True).click()
            page.get_by_text('第 1 次 · 新上下文 · 中断后自动续跑：StreamChunkTimeoutError').wait_for()
            page.get_by_text('最近工具失败：execute_command [connection_failed] curl exited 7').wait_for()
            assert page.get_by_role('link', name='导出工作记录（Markdown）').get_attribute('href').endswith('/requests/leader%3At/journal')
            assert not [a for a in actions if a['action'] == 'environment']  # loaded only when opened
            page.get_by_text('项目环境（端口、地址与命令）', exact=True).click()
            page.get_by_text('平台保留端口：17420, 1420', exact=False).wait_for()
            page.get_by_text('python3 app.py', exact=True).wait_for()
            page.get_by_text('运行中（backend 启动，任务结束后保留） · 已验证', exact=True).wait_for()
            page.get_by_role('button', name='停止', exact=True).click()
            page.get_by_text('未运行 · 已验证', exact=True).wait_for()
            assert dict(action='stop_service', payload='api') in actions
            page.get_by_text('登记或修改服务', exact=True).click()
            page.get_by_label('服务名称（如 api、web、db）').fill('web')
            page.get_by_role('button', name='保存', exact=True).click()
            page.get_by_text('web', exact=True).wait_for()
            assert [a['payload'] for a in actions if a['action'] == 'register_service'] == [{'name': 'web'}]
            page.screenshot(path=str(tmp_path / 'environment.png'))
            assert not errors
            browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join()
