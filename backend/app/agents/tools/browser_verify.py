"""Browser evidence collected in a sandboxed, fresh Playwright context."""
import hashlib
import json
import re
from pathlib import Path
import sys
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.agents.tools.validation import tool, failure_result
from app.agents.context_policy import RECOMPUTABLE
from app.agents.tools._workspace import resolve_allowed, workspace_root
from app.execution.browser_runner import MAX_TILES, TILE_HEIGHT
from app.execution.processes import processes


MAX_ACTIONS = 30
# Whose checks keep a screenshot unless the call says otherwise: looking at the page is
# the testing role's work; a developer's check is a smoke test (no errors, the text is there).
SCREENSHOT_ROLES = {'testing'}


class BrowserAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['click', 'fill', 'press', 'wait_visible', 'assert_text']
    selector: str = Field(min_length=1, max_length=1000)
    value: str = Field(default='', max_length=4000)


class BrowserCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(max_length=2000)
    actions: list[BrowserAction] = Field(default_factory=list, max_length=MAX_ACTIONS)
    allowed_origins: list[str] = Field(default_factory=list, max_length=10)
    expect_errors: bool = False
    screenshot: bool | None = Field(default=None, description='Save and show a full-page screenshot. Default: on for the testing role, off for others')

    @model_validator(mode='after')
    def http_only(self):
        for url in [self.url, *self.allowed_origins]:
            parts = urlsplit(url)
            if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
                raise ValueError('Only HTTP(S) URLs without embedded credentials are allowed')
        if any(urlsplit(value).path not in {'', '/'} or urlsplit(value).query or urlsplit(value).fragment for value in self.allowed_origins):
            raise ValueError('allowed_origins must contain origins, without paths or queries')
        return self


@tool(args_schema=BrowserCheck, context_policy=RECOMPUTABLE)
async def browser_verify(url: str, actions: list[BrowserAction] = [], allowed_origins: list[str] = [],
                         expect_errors: bool = False, screenshot: bool | None = None) -> dict:
    """Open a page, run ordered selector actions, collect text and console/page/HTTP
    errors. With a screenshot (the testing role's default; others pass screenshot=true
    when a visible defect needs a look) the full page is saved in docs/testing and shown
    to you with the result: judge layout, styling and content from it, not more checks.
    Fresh sandboxed Chromium, no personal cookies; needs Playwright + Chromium on the
    backend host and reviewed network access, never installed implicitly. List other
    HTTP origins (e.g. the backend API). One session per call; navigation retries
    briefly while the server starts. At most 30 actions per call; split a longer flow.
    Take selectors and expected text from the page source or the result. wait_visible
    and assert_text accept any element the selector matches; click, fill and press need
    exactly one (append " >> nth=0" to take the first). assert_text waits for the text
    and ignores spacing and line breaks. A failed check returns the page text and its
    element outline (dom) to choose the next selector from. For an intended failure
    path (e.g. API stopped) set expect_errors=true: errors are reported but do not fail
    the check; actions and assertions still must pass.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {**failure_result('Playwright is missing', 'environment_unavailable', 'missing_playwright',
            'Prepare Playwright on the backend host within authorized scope: uv sync --extra browser; then uv run playwright install chromium.',
            'after_environment_change'), 'passed': False}
    async with async_playwright() as playwright:
        browser_path = Path(playwright.chromium.executable_path).resolve()
    if not browser_path.is_file():
        return {**failure_result('Chromium is missing', 'environment_unavailable', 'missing_chromium',
            'Prepare Chromium on the backend host within authorized scope: uv run playwright install chromium.',
            'after_environment_change'), 'passed': False}
    if screenshot is None:
        from app.collaboration.tools import active
        screenshot = getattr(active.get(), 'member', None) in SCREENSHOT_ROLES
    target = None
    if screenshot:
        target = resolve_allowed(f'docs/testing/browser-{uuid4().hex}.png', ('docs/testing',))
        target.parent.mkdir(parents=True, exist_ok=True)
    runner = Path(__file__).resolve().parents[2] / 'execution/browser_runner.py'
    browser_cache = next((p.parent for p in browser_path.parents if p.name.startswith('chromium-')), browser_path.parent)
    spec = {'url': url, 'browser_cache': str(browser_cache), 'actions': [a.model_dump() if isinstance(a, BrowserAction) else a for a in actions],
            'allowed_origins': [origin.rstrip('/') for origin in allowed_origins], 'screenshot': str(target) if target else None,
            'expect_errors': expect_errors}
    # Playwright uses multiple sibling browser bundles (Chromium, headless shell, ffmpeg).
    result = await processes.run([sys.executable, '-I', str(runner), json.dumps(spec)], timeout=300,
        network=True, reason='Inspect page and interactions; collect browser evidence', extra_reads=(runner, browser_cache), browser=True)
    prefix = 'WDRUNGE_BROWSER_RESULT='
    lines = [line[len(prefix):] for line in result['output'].splitlines() if line.startswith(prefix)]
    if result.get('error'):
        return {**result, 'passed': False}
    try:
        evidence = json.loads(lines[-1]) if lines else None
        if not isinstance(evidence, dict) or not isinstance(evidence.get('passed'), bool):
            raise ValueError('Browser returned no valid verification result')
    except (ValueError, TypeError):
        return {**result, **failure_result('Browser returned no valid verification result', 'internal_error',
            'invalid_browser_result', 'Inspect browser process output and the runner protocol; report the tool defect rather than changing page selectors.',
            'do_not_retry_unchanged'), 'passed': False}
    if not evidence['passed'] and not evidence.get('error_details'):
        evidence.update(failure_result('Page verification found browser/HTTP errors', 'verification_failed',
            'page_errors', 'Inspect console, page_errors, http_errors and screenshot. Check service readiness or report the application defect before rechecking.',
            'inspect_before_retry'))
    shots = [Path(p) for p in evidence.pop('screenshots', None) or [] if isinstance(p, str)]
    shots = [named_by_content(p, url) for p in shots if target and p.is_file() and p.parent == target.parent]
    if shots:
        # Workspace-relative: what reports cite, and how page_images finds them for the model.
        names = [str(p.relative_to(workspace_root().resolve())) for p in shots]
        evidence['screenshot'] = names[0]
        if len(names) > 1:
            evidence['screenshots'] = names
        if isinstance(evidence.get('page_height'), int) and evidence['page_height'] > TILE_HEIGHT * MAX_TILES:
            evidence['screenshot_covers'] = f'top {TILE_HEIGHT * MAX_TILES}px of {evidence["page_height"]}px'
    evidence.pop('page_height', None)
    return evidence


def page_slug(url: str) -> str:
    """The page a URL shows, as a word for a file name: "/" is home, "/admin/login" is
    admin-login."""
    parts = urlsplit(url)
    words = re.sub(r'[^a-z0-9]+', '-', f'{parts.path} {parts.fragment}'.lower()).strip('-')
    return (words or 'home')[:32].strip('-') or 'home'


def named_by_content(screenshot: Path, url: str = '') -> Path:
    """shot-<page>-<4 hex of the content>.png: a report cites it by copying the name, so
    it is short and says which page it shows (a 16-digit hash was copied with one digit
    wrong, and the delivery came back for it). Identical screenshots share one path, so
    repeated checks of an unchanged page leave one file; delivery merges only the
    screenshots a report or result cites."""
    data = screenshot.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    for length in (4, 8, 16):
        named = screenshot.with_name(f'shot-{page_slug(url)}-{digest[:length]}.png')
        if not named.exists():
            screenshot.rename(named)
            return named
        if named.read_bytes() == data:
            break  # the same picture: one file
    screenshot.unlink()
    return named
