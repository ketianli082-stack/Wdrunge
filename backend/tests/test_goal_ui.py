"""Exercise the built goal controls and evidence forms against a mocked local API."""
import json
from pathlib import Path
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest


def test_goal_controls_and_evidence(tmp_path):
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright
    root = Path(__file__).resolve().parents[2] / 'frontend/dist'
    if not (root/'index.html').exists():
        pytest.skip('Build frontend first')
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    conversation = dict(id='c', project_id='p', title='Goal test', status='idle', project_name='Test project', workspace_path='/tmp/test',
                        messages=[], activities=[], created_at='2026-10-01T00:00:00Z', updated_at='2026-10-01T00:00:00Z')
    goal = dict(id='c:g', conversation='c', objective='交付可验证的功能', criteria=['集成测试通过'], state='active', reason='', evidence={}, next_reminder_at=1800000000, reminder_count=1)
    task = dict(id='leader:t', goal_id='c:g', acceptance_criteria=['接口可用'], conversation='c', summary='实现接口', sender='leader', recipient='backend', state='review', goal='实现接口并验证', result='GET /health 返回 200', dependencies=[], delivered=[])
    state = dict(document=[], request=[task], message=[], goal=[goal], settings=[])
    actions=[]
    def api(route):
        path=route.request.url.split('/api/',1)[1].split('?',1)[0];data={}
        if route.request.method=='OPTIONS':
            route.fulfill(status=204,headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*','Access-Control-Allow-Methods':'*'});return
        if path=='projects': data=[dict(id='p', name='Test project',workspace_path='/tmp/test',conversations=[conversation])]
        elif path=='conversations/c': data=conversation
        elif path=='team': data=dict(agents=[],usage=dict(agents=[],total_tokens=0))
        elif path=='projects/p/collaboration': data=state
        elif path=='projects/p/collaboration/commands':
            body=route.request.post_data_json;actions.append(body)
            assert body['conversation']=='c'
            action,payload=body['action'],body['payload']
            if action=='update_goal': goal.update(state=payload['state'],reason=payload['reason'])
            elif action=='complete_task':
                assert payload['evidence']=={'接口可用':'GET /health 验证返回 200'}
                task['state']='completed'
            elif action=='complete_goal':
                assert payload['evidence']=={'集成测试通过':'集成测试 12 项全部通过'}
                goal.update(state='completed',evidence=payload['evidence'])
        elif path.endswith('/files'): data=dict(files=[])
        elif path.endswith('/events'): data=dict(events=[],cursor=0,has_more=False)
        elif path=='execution/capabilities': data=dict(pending=0)
        route.fulfill(status=200,content_type='application/json',body=json.dumps(data),headers={'Access-Control-Allow-Origin':'*'})
    try:
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file(): pytest.skip('Optional Chromium unavailable')
            browser=playwright.chromium.launch();page=browser.new_page(viewport=dict(width=1440,height=1000))
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.route('**/api/**',api);page.goto(f'http://127.0.0.1:{server.server_port}')
            page.get_by_role('button',name='任务与协作',exact=True).click()
            page.get_by_role('button',name='暂停目标',exact=True).click()
            page.get_by_role('button',name='恢复目标',exact=True).click()
            page.get_by_text('逐项验收',exact=True).click()
            assert page.get_by_role('button',name='确认验收',exact=True).is_disabled()
            page.get_by_label('接口可用',exact=True).fill('GET /health 验证返回 200')
            page.get_by_role('button',name='确认验收',exact=True).click()
            page.get_by_text('验收目标',exact=True).click()
            page.get_by_label('集成测试通过',exact=True).fill('集成测试 12 项全部通过')
            page.screenshot(path=str(tmp_path/'goal-acceptance.png'))
            page.get_by_role('button',name='确认验收',exact=True).click()
            page.get_by_text('已完成',exact=True).wait_for()
            assert [a['action'] for a in actions]==['update_goal','update_goal','complete_task','complete_goal']
            assert not errors
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join()
