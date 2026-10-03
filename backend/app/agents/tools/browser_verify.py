"""Browser evidence collected in a sandboxed, fresh Playwright context."""
import hashlib
import json
from pathlib import Path
import sys
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.agents.tools.validation import tool, failure_result
from app.agents.tools._workspace import resolve_allowed
from app.execution.processes import processes


class BrowserAction(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    action: Literal['click', 'fill', 'press', 'wait_visible', 'assert_text']
    selector: str = Field(min_length=1, max_length=1000)
    value: str = Field(default='', max_length=4000)


class BrowserCheck(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    url: str = Field(max_length=2000)
    actions: list[BrowserAction] = Field(default_factory=list, max_length=20)
    allowed_origins: list[str] = Field(default_factory=list, max_length=10)
    expect_errors: bool = False

    @model_validator(mode='after')
    def http_only(self):
        for url in [self.url, *self.allowed_origins]:
            parts = urlsplit(url)
            if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
                raise ValueError('Only HTTP(S) URLs without embedded credentials are allowed')
        if any(urlsplit(value).path not in {'', '/'} or urlsplit(value).query or urlsplit(value).fragment for value in self.allowed_origins):
            raise ValueError('allowed_origins must contain origins, without paths or queries')
        return self


@tool(args_schema=BrowserCheck)
async def browser_verify(url: str, actions: list[BrowserAction] = [], allowed_origins: list[str] = [],
                         expect_errors: bool = False) -> dict:
    """Open a page, perform ordered selector actions, inspect text/console/page and HTTP
    errors, and save a viewport PNG in docs/testing. Uses a fresh sandboxed Chromium
    session with no personal cookies. Requires Playwright + its Chromium browser on
    the backend host and reviewed network access; never installs them implicitly.
    Other HTTP origins must be listed (e.g. the backend API used by the frontend).
    All actions share this one session; a later call starts a new session.
    Navigation retries for a few seconds while the page server is still starting.
    Take selectors and expected text from the page source or a previous result's text;
    an assert_text failure reports the actual text. For an intended failure path (e.g.
    the API is stopped) set expect_errors=true: console/page/HTTP errors are then
    reported but do not fail the check, while actions and assertions still must pass.
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
    screenshot = resolve_allowed(f'docs/testing/browser-{uuid4().hex}.png', ('docs/testing',))
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    runner = Path(__file__).resolve().parents[2] / 'execution/browser_runner.py'
    browser_cache = next((p.parent for p in browser_path.parents if p.name.startswith('chromium-')), browser_path.parent)
    spec = {'url': url, 'browser_cache': str(browser_cache), 'actions': [a.model_dump() if isinstance(a, BrowserAction) else a for a in actions],
            'allowed_origins': [origin.rstrip('/') for origin in allowed_origins], 'screenshot': str(screenshot),
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
    if screenshot.is_file():
        evidence['screenshot'] = str(named_by_content(screenshot))
    return evidence


def named_by_content(screenshot: Path) -> Path:
    """Identical screenshots share one path, so repeated checks of an unchanged page
    leave one file; delivery merges only the screenshots a report or result cites."""
    named = screenshot.with_name(f'browser-{hashlib.sha256(screenshot.read_bytes()).hexdigest()[:16]}.png')
    if named.exists():
        screenshot.unlink()
    else:
        screenshot.rename(named)
    return named
