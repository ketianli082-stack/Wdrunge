"""Switching views cannot detach stop controls; app reload requires manual resume."""
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest


def test_stop_survives_navigation_and_reload_pauses(tmp_path):
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright
    root=Path(__file__).resolve().parents[2]/'frontend/dist'
    if not (root/'index.html').exists():pytest.skip('Build frontend first')
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(root)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    conversations={name:dict(id=name,project_id='p',project_name='Project',title=f'Chat {name}',workspace_path='/tmp/p',messages=[],activities=[],status='active',created_at='2026-10-02T00:00:00Z',updated_at='2026-10-02T00:00:00Z') for name in ('a','b')}
    states={name:dict(running=name=='a',paused=False,stopping=False,pending_tasks=int(name=='a'),active_goals=0) for name in conversations}
    actions=[];fail_stop=True
    def api(route):
        nonlocal fail_stop
        path=route.request.url.split('/api/',1)[1].split('?',1)[0];data={};status=200
        headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*','Access-Control-Allow-Methods':'*'}
        if route.request.method=='OPTIONS':route.fulfill(status=204,headers=headers);return
        if path=='execution/pause':
            for state in states.values():
                if state['running']:state.update(running=False,paused=True)
            data={'paused_conversations':1}
        elif path=='projects':data=[dict(id='p',name='Project',workspace_path='/tmp/p',conversations=list(conversations.values()))]
        elif path=='team':data=dict(agents=[],usage=dict(agents=[],total_tokens=0))
        elif path=='execution/capabilities':data={'pending':0}
        elif path.startswith('conversations/'):
            parts=path.split('/');name=parts[1]
            if len(parts)==2:data=conversations[name]
            elif parts[2]=='events':data=dict(events=[],cursor=0,has_more=False)
            elif parts[2]=='execution':
                if len(parts)==4:
                    actions.append((name,parts[3]))
                    if parts[3]=='resume':states[name].update(running=True,paused=False)
                    elif fail_stop:status=500;data={'detail':'停止失败，请重试'};fail_stop=False
                    else:states[name].update(running=False,paused=False,pending_tasks=0)
                if status==200:data=states[name]
        route.fulfill(status=status,content_type='application/json',body=json.dumps(data),headers=headers)
    try:
        with sync_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Chromium unavailable')
            browser=pw.chromium.launch();page=browser.new_page(viewport=dict(width=1440,height=1000));errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('**/api/**',api);page.goto(f'http://127.0.0.1:{server.server_port}')
            page.get_by_role('button',name='继续任务',exact=True).wait_for()
            assert not actions
            page.get_by_role('button',name='继续任务',exact=True).click()
            page.get_by_role('button',name='停止生成',exact=True).wait_for()
            page.get_by_role('tab',name='工作日志').click();page.get_by_role('tab',name='团队群聊').click()
            page.get_by_role('button',name='停止生成',exact=True).wait_for()
            page.get_by_role('button',name='Chat b',exact=False).click()
            page.get_by_role('button',name='发送',exact=True).wait_for()
            assert page.get_by_role('button',name='停止生成',exact=True).count()==0
            page.get_by_role('button',name='Chat a',exact=False).click()
            page.get_by_role('button',name='停止生成',exact=True).click()
            page.get_by_role('alert').filter(has_text='停止失败').wait_for()
            page.get_by_role('button',name='停止生成',exact=True).click()
            page.get_by_role('button',name='发送',exact=True).wait_for()
            assert actions==[('a','resume'),('a','stop'),('a','stop')]
            # Reopening a window while its separate backend is still running must pause it.
            states['a'].update(running=True,paused=False,pending_tasks=1)
            page.reload()
            page.get_by_role('button',name='继续任务',exact=True).wait_for()
            assert len(actions)==3 and not states['a']['running']
            assert not errors
            browser.close()
    finally:server.shutdown();server.server_close();thread.join()
