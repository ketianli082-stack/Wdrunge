"""One disposable browser session, invoked only inside the execution sandbox."""
import asyncio
import json
import os
import sys
from urllib.parse import urlsplit


class AssertionFailed(ValueError):
    pass


def browser_failure(exc, stage, action_index=None):
    """Classify at the failing stage; never call a browser API defect a bad selector."""
    from playwright.async_api import Error, TimeoutError
    message = str(exc)[:2000]
    if isinstance(exc, TypeError):
        category, code, policy = 'internal_error', 'browser_runtime_error', 'do_not_retry_unchanged'
        advice = 'Report this browser tool/runtime defect. Preserve the error and use another supported verification method; changing selectors will not fix it.'
    elif stage == 'navigation' and isinstance(exc, Error) and any(s in message for s in (
            'ERR_CONNECTION_REFUSED', 'ERR_CONNECTION_RESET', 'ERR_NAME_NOT_RESOLVED', 'ERR_EMPTY_RESPONSE')):
        category, code, policy = 'service_unavailable', 'navigation_failed', 'after_service_ready'
        advice = 'Check the URL and manage_process status/logs. Start or wait for the authorized service and verify readiness before retrying.'
    elif isinstance(exc, TimeoutError):
        category, code, policy = 'verification_failed', 'navigation_timeout' if stage == 'navigation' else 'action_timeout', 'inspect_before_retry'
        advice = 'Inspect the screenshot, page/HTTP errors and service readiness. For an action timeout inspect the actual DOM/selector and loading state; do not repeat unchanged checks.'
    elif isinstance(exc, AssertionFailed):
        category, code, policy = 'verification_failed', 'assertion_failed', 'after_state_change'
        advice = 'Inspect the actual page and expected behavior; report or fix the application discrepancy, then reverify. Do not alter the expected result merely to pass.'
    else:
        category, code, policy = 'internal_error', 'browser_runtime_error', 'do_not_retry_unchanged'
        advice = 'Inspect the browser/runtime error and environment. Preserve evidence and report the blocker or use another supported verification method.'
    return {'error': message, 'error_details': {'category': category, 'code': code,
        'retry_policy': policy, 'next_action': advice, 'stage': stage,
        'action_index': action_index, 'exception_type': type(exc).__name__}, 'passed': False}


async def run(spec):
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = spec['browser_cache']
    from playwright.async_api import async_playwright, Error
    console, errors, responses = [], [], []
    def append(items, value):
        if len(items) < 100:
            items.append(value)
    def origin(url):
        parts = urlsplit(url)
        scheme = {'ws': 'http', 'wss': 'https'}.get(parts.scheme, parts.scheme)
        return f'{scheme}://{parts.netloc}'
    allowed = {origin(spec['url']), *spec['allowed_origins']}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width': 1440, 'height': 1000}, service_workers='block', accept_downloads=False)
        async def route(request):
            if origin(request.request.url) not in allowed:
                append(errors, f'Blocked unapproved origin: {origin(request.request.url)}')
                return await request.abort()
            await request.continue_()
        await context.route('**/*', route)
        async def websocket(route):
            if origin(route.url) not in allowed:
                append(errors, f'Blocked unapproved WebSocket origin: {origin(route.url)}')
                await route.close()
            else:
                route.connect_to_server()
        await context.route_web_socket('**/*', websocket)
        page = await context.new_page()
        page.set_default_timeout(10000)
        page.on('console', lambda message: append(console, {'level': message.type, 'text': message.text[:2000]}))
        page.on('pageerror', lambda error: append(errors, str(error)[:2000]))
        page.on('requestfailed', lambda request: append(errors, f'{request.method} {request.url[:1000]}: {request.failure}'))
        page.on('response', lambda response: append(responses, {'url': response.url[:1000], 'status': response.status}) if response.status >= 400 else None)
        results = []
        stage, action_index = 'navigation', None
        try:
            for attempt in range(6):
                try:
                    response = await page.goto(spec['url'], wait_until='domcontentloaded', timeout=30000)
                    break
                except Error as exc:
                    # A service that is still starting refuses connections briefly.
                    if attempt == 5 or 'ERR_CONNECTION_REFUSED' not in str(exc):
                        raise
                    # The refused attempt is not evidence about the page that loads later.
                    errors.clear()
                    console.clear()
                    await asyncio.sleep(1)
            stage = 'action'
            for action_index, action in enumerate(spec['actions']):
                locator = page.locator(action['selector'])
                kind = action['action']
                if kind == 'click':
                    await locator.click()
                elif kind == 'fill':
                    await locator.fill(action['value'])
                elif kind == 'press':
                    await locator.press(action['value'])
                elif kind == 'wait_visible':
                    await locator.wait_for(state='visible')
                elif kind == 'assert_text':
                    actual = await locator.inner_text()
                    if action['value'] not in actual:
                        raise AssertionFailed(f"Expected text {action['value']!r}; got {actual[:500]!r}")
                results.append({'action': kind, 'selector': action['selector'], 'passed': True})
            stage, action_index = 'screenshot', None
            await page.screenshot(path=spec['screenshot'], full_page=False)
            return {'url': page.url, 'title': await page.title(), 'status': response.status if response else None,
                    'text': (await page.locator('body').inner_text())[:12000], 'actions': results,
                    'console': console, 'page_errors': errors, 'http_errors': responses,
                    'screenshot': spec['screenshot'],
                    'expected_errors': bool(spec.get('expect_errors')),
                    'passed': bool(spec.get('expect_errors')) or not errors and not responses and not any(item['level'] == 'error' for item in console)}
        except Exception as exc:
            try:
                await page.screenshot(path=spec['screenshot'], full_page=False)
            except Exception:
                pass
            return {**browser_failure(exc, stage, action_index), 'actions': results, 'console': console,
                    'page_errors': errors, 'http_errors': responses}
        finally:
            await browser.close()


if __name__ == '__main__':
    try:
        result = asyncio.run(run(json.loads(sys.argv[1])))
    except Exception as error:
        result = browser_failure(error, 'startup')
    result['counts'] = {key: len(result.get(key, [])) for key in ('actions', 'console', 'page_errors', 'http_errors')}
    # One bounded UTF-8 record fits the process result's byte budget even on noisy
    # pages. Preserve pass/fail and original counts when trimming evidence.
    def encode():
        return json.dumps(result, ensure_ascii=False)
    while len(encode().encode()) > 24000:
        result['truncated'] = True
        candidates = [key for key, value in result.items() if isinstance(value, (list, str)) and len(value) > 1]
        key = max(candidates, key=lambda k: len(json.dumps(result[k]).encode()))
        result[key] = result[key][:max(1, len(result[key]) // 2)]
    print('WDRUNGE_BROWSER_RESULT=' + encode())
