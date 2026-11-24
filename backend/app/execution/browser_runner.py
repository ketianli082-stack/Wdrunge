"""One disposable browser session, invoked only inside the execution sandbox."""
import asyncio
import json
import os
import sys
from urllib.parse import urlsplit


class AssertionFailed(ValueError):
    pass


class SelectorProblem(ValueError):
    """The selector is what has to change, not the page: it matches several elements
    where one is needed, or is not valid selector syntax."""
    def __init__(self, message, code, advice):
        super().__init__(message)
        self.code, self.advice = code, advice


ACTION_TIMEOUT = 10  # seconds for an element to appear and take an action
TEXT_WAIT = 5        # seconds for an element that exists to show the expected text
FAILURE_TEXT = 3000  # characters of page text returned with a failed check
OUTLINE_LIMIT = 6000

# The first matches of a selector, each as its opening tag and the start of its text.
DESCRIBE = r"""els => els.slice(0, 5).map(el => {
  const tag = el.cloneNode(false).outerHTML.replace(/<\/[^>]+>$/, '');
  const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  return (tag.length > 160 ? tag.slice(0, 160) + '…' : tag) + (text ? ' "' + (text.length > 60 ? text.slice(0, 60) + '…' : text) + '"' : '');
})"""

# What a failed check needs to choose a selector that exists: the page's elements as an
# indented outline (tag, id, classes, the attributes selectors are written with, own
# text). Wrappers holding a single element share a line; repeated siblings of the same
# build (cards, rows) are shown once with a count, long runs of leaves as their first
# eight; hidden subtrees are named, not opened.
OUTLINE = r"""limit => {
  const KEEP = ['data-testid', 'data-test', 'role', 'name', 'type', 'href', 'placeholder', 'aria-label', 'for', 'title'];
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'LINK', 'META', 'BR', 'HR']);
  const LEAF = new Set(['svg', 'SVG', 'CANVAS', 'IFRAME', 'VIDEO', 'SELECT', 'PRE']);
  const clip = (text, n) => text.length > n ? text.slice(0, n) + '…' : text;
  const shape = el => el.tagName.toLowerCase() + [...el.classList].slice(0, 4).map(name => '.' + name).join('');
  const kids = el => LEAF.has(el.tagName) ? [] : [...el.children].filter(child => !SKIP.has(child.tagName));
  const build = el => shape(el) + '(' + kids(el).map(shape).join(',') + ')';
  const label = el => {
    let out = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + [...el.classList].slice(0, 4).map(name => '.' + name).join('');
    for (const name of KEEP) { const value = el.getAttribute(name); if (value) out += `[${name}="${clip(value, 40)}"]`; }
    if (/^(INPUT|TEXTAREA)$/.test(el.tagName) && el.type !== 'password' && el.value) out += `[value="${clip(el.value, 40)}"]`;
    if (el.disabled) out += '[disabled]';
    return out;
  };
  const own = el => clip([...el.childNodes].filter(node => node.nodeType === 3).map(node => node.textContent.trim()).filter(Boolean).join(' '), 40);
  const hidden = el => { const style = getComputedStyle(el); return style.display === 'none' || style.visibility === 'hidden'; };
  const lines = []; let size = 0, cut = false;
  const emit = line => { if (size + line.length > limit) { cut = true; return false; } lines.push(line); size += line.length + 1; return true; };
  const walk = (el, depth) => {
    let line = label(el), text = own(el), node = el, unseen = hidden(el);
    while (!unseen && !text) {
      const inner = kids(node);
      if (inner.length !== 1) break;
      node = inner[0]; unseen = hidden(node); line += ' > ' + label(node); text = own(node);
    }
    if (!emit('  '.repeat(depth) + line + (unseen ? ' (hidden)' : '') + (text ? ` "${text}"` : '')) || unseen) return;
    const inner = kids(node);
    for (let i = 0; i < inner.length && !cut;) {
      let j = i + 1;
      while (j < inner.length && build(inner[j]) === build(inner[i])) j++;
      // Leaves cost a line each and differ in what selectors use (name, href, text).
      const shown = Math.min(j - i, kids(inner[i]).length ? 1 : 8);
      for (let k = 0; k < shown && !cut; k++) walk(inner[i + k], depth + 1);
      if (j - i > shown && !cut) emit('  '.repeat(depth + 1) + `… ${j - i - shown} more ${shape(inner[i])} built like the one above`);
      i = j;
    }
  };
  if (document.body) walk(document.body, 0);
  if (cut) lines.push('… (outline cut here)');
  return lines.join('\n');
}"""


def squeeze(text):
    """Text as a reader sees it: runs of spaces and line breaks are one space."""
    return ' '.join(text.split())


async def matched_text(locator, expected):
    """Wait for any element the selector matches to contain the expected text. Returns
    the number of matches and a note when only the whitespace differs; raises with the
    texts actually found. innerText breaks lines between blocks ("分类：\n前端"), which is
    layout, not content, so spacing is never what fails a text check."""
    loop = asyncio.get_running_loop()
    appear, settle = loop.time() + ACTION_TIMEOUT, None
    want, bare = squeeze(expected), ''.join(expected.split())
    while True:
        texts = await locator.evaluate_all('els => els.slice(0, 50).map(el => el.innerText ?? el.textContent ?? "")')
        if any(want in squeeze(text) for text in texts):
            return len(texts), None
        if bare and any(bare in ''.join(text.split()) for text in texts):
            return len(texts), 'matched ignoring spaces and line breaks'
        now = loop.time()
        if texts and settle is None:
            settle = now + TEXT_WAIT
        if now >= (settle if texts else appear):
            break
        await asyncio.sleep(0.1)
    if not texts:
        from playwright.async_api import TimeoutError
        raise TimeoutError(f'No element matched within {ACTION_TIMEOUT}s')
    got = '; '.join(f'{index}) {squeeze(text)[:300]!r}' for index, text in enumerate(texts[:5])) if len(texts) > 1 else repr(squeeze(texts[0])[:500])
    raise AssertionFailed(f'Expected text {expected!r}; got {got}' + (f' ({len(texts)} elements match the selector)' if len(texts) > 1 else ''))


async def act(page, action):
    """Run one action; returns facts to report beside it."""
    from playwright.async_api import Error
    locator, kind = page.locator(action['selector']), action['action']
    try:
        if kind == 'wait_visible':
            # A check that only looks is satisfied by any match.
            await locator.locator('visible=true').first.wait_for(state='visible')
            count = await locator.count()
            return {'matches': count} if count > 1 else {}
        if kind == 'assert_text':
            count, note = await matched_text(locator, action['value'])
            return {**({'matches': count} if count > 1 else {}), **({'note': note} if note else {})}
        if kind == 'click':
            await locator.click()
        elif kind == 'fill':
            await locator.fill(action['value'])
        elif kind == 'press':
            await locator.press(action['value'])
        return {}
    except Error as exc:
        message = str(exc)
        if 'strict mode violation' in message:
            count = await locator.count()
            listed = '\n'.join(f'  {index}) {item}' for index, item in enumerate(await locator.evaluate_all(DESCRIBE)))
            raise SelectorProblem(
                f'{kind} needs one element, but selector {action["selector"]!r} matches {count}:\n{listed}'
                + (f'\n  … and {count - 5} more' if count > 5 else ''), 'ambiguous_selector',
                f'Pick one by its number in the list: {action["selector"] + " >> nth=0"!r} is the first. '
                'Or narrow the selector with an attribute or :has-text("…") taken from the list.') from exc
        if any(mark in message for mark in ('while parsing', 'Unexpected token', 'Malformed selector', 'Unknown engine', 'Unsupported token')):
            raise SelectorProblem(
                f'Selector {action["selector"]!r} is not valid: {message.splitlines()[0][:300]}', 'invalid_selector',
                'Write a CSS selector. To match by text put :has-text("…") on an element (tr:has-text("草稿") button); '
                'to continue from a match use " >> " (tr:has-text("草稿") >> text=撤回); " >> nth=0" picks the first match.') from exc
        raise


# The model reviews the page from these images. A provider scales any image above
# about 2.6M pixels down to that size (~2,560 tokens), so a long page is cut into
# tiles: each tile is scaled at most ~0.64x and body text stays legible.
TILE_HEIGHT = 4000
MAX_TILES = 3


async def capture(page, base):
    """Full-page screenshot as up to MAX_TILES files (base, base-2, ...); returns the
    paths and the page height, which exceeds the tiles' coverage when cut off."""
    width, height = await page.evaluate(
        '[document.documentElement.clientWidth, Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)]')
    width, height = max(1, int(width)), max(1, int(height))
    if height <= TILE_HEIGHT:
        await page.screenshot(path=base, full_page=True)
        return [base], height
    stem = base[:-len('.png')]
    paths = []
    for index, top in enumerate(range(0, min(height, TILE_HEIGHT * MAX_TILES), TILE_HEIGHT)):
        path = base if index == 0 else f'{stem}-{index + 1}.png'
        await page.screenshot(path=path, full_page=True,
                              clip={'x': 0, 'y': top, 'width': width, 'height': min(TILE_HEIGHT, height - top)})
        paths.append(path)
    return paths, height


def browser_failure(exc, stage, action_index=None, matches=None):
    """Classify at the failing stage; never call a browser API defect a bad selector.
    `matches`: how many elements the failing action's selector matched, when known."""
    from playwright.async_api import Error, TimeoutError
    message = str(exc)[:2000]
    if isinstance(exc, SelectorProblem):
        category, code, policy, advice = 'invalid_arguments', exc.code, 'correct_arguments', exc.advice
    elif isinstance(exc, TimeoutError) and stage == 'action' and matches == 0:
        category, code, policy = 'verification_failed', 'selector_not_found', 'inspect_before_retry'
        message = f'The selector matched no element. {message}'
        advice = ('Take a selector from "dom" in this result (the outline of the page as it is now) or check "text"; '
                  'if the element should exist, the page did not render it: see console, page_errors and http_errors.')
    elif isinstance(exc, TimeoutError) and stage == 'action' and matches:
        category, code, policy = 'verification_failed', 'action_timeout', 'inspect_before_retry'
        message = f'The selector matched {matches} element(s), but the action did not complete. {message}'
        advice = 'The call log above says why (hidden, disabled, or covered by another element); "dom" marks hidden elements.'
    elif isinstance(exc, TypeError):
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
    return {'error': message, 'error_details': {'category': category, 'code': code, 'next_action': advice, 'stage': stage,
        **({'action_index': action_index} if action_index is not None else {}),
        **({'selector_matches': matches} if matches is not None else {})}, 'passed': False}


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
        page.set_default_timeout(ACTION_TIMEOUT * 1000)
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
                facts = await act(page, action)
                results.append({'action': action['action'], 'selector': action['selector'], 'passed': True, **facts})
            stage, action_index = 'screenshot', None
            shots = {}
            if spec.get('screenshot'):
                screenshots, page_height = await capture(page, spec['screenshot'])
                shots = {'screenshots': screenshots, 'page_height': page_height}
            return {'url': page.url, 'title': await page.title(), 'status': response.status if response else None,
                    'text': (await page.locator('body').inner_text())[:12000], 'actions': results,
                    'console': console, 'page_errors': errors, 'http_errors': responses, **shots,
                    'expected_errors': bool(spec.get('expect_errors')),
                    'passed': bool(spec.get('expect_errors')) or not errors and not responses and not any(item['level'] == 'error' for item in console)}
        except Exception as exc:
            shots, matches, seen = {}, None, {}
            if stage == 'action' and not isinstance(exc, SelectorProblem):
                try:
                    matches = await page.locator(spec['actions'][action_index]['selector']).count()
                except Exception:
                    pass
            if stage != 'navigation':
                # The page as it is at the failure: what the next selector or the fix is chosen from.
                try:
                    seen = {'url': page.url, 'title': await page.title(),
                            'text': (await page.locator('body').inner_text(timeout=3000))[:FAILURE_TEXT],
                            'dom': await page.evaluate(OUTLINE, OUTLINE_LIMIT)}
                except Exception:
                    pass
            try:
                if spec.get('screenshot'):
                    screenshots, page_height = await capture(page, spec['screenshot'])
                    shots = {'screenshots': screenshots, 'page_height': page_height}
            except Exception:
                pass
            return {**browser_failure(exc, stage, action_index, matches), **seen, 'actions': results, 'console': console,
                    'page_errors': errors, 'http_errors': responses, **shots}
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
        candidates = [key for key, value in result.items()
                      if isinstance(value, (list, str)) and len(value) > 1 and key != 'screenshots']
        key = max(candidates, key=lambda k: len(json.dumps(result[k]).encode()))
        result[key] = result[key][:max(1, len(result[key]) // 2)]
    print('WDRUNGE_BROWSER_RESULT=' + encode())
