"""Retry transient model-provider failures instead of failing the whole task.

A stalled stream, dropped connection, rate limit or provider 5xx says nothing about
the task. Retrying the same model call from the same graph state repeats no tool
side effects. Invalid requests and context overflow are not retried here.
"""
import asyncio
from collections import deque
import logging
import os
import time

logger = logging.getLogger(__name__)

TRANSIENT_NAMES = {
    'StreamChunkTimeoutError', 'APIConnectionError', 'APITimeoutError', 'RateLimitError', 'InternalServerError',
    'ServiceUnavailableError', 'ReadTimeout', 'ConnectTimeout', 'ConnectError', 'ReadError', 'RemoteProtocolError',
    'PoolTimeout', 'TimeoutError',
}


def is_transient(error: BaseException) -> bool:
    if isinstance(error, asyncio.CancelledError):
        return False
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if any(cls.__name__ in TRANSIENT_NAMES for cls in type(error).__mro__):
            return True
        status = getattr(error, 'status_code', None)
        if isinstance(status, int) and (status == 429 or status >= 500):
            return True
        error = error.__cause__ or error.__context__
    return False


def retry_attempts() -> int:
    try:
        return max(1, int(os.getenv('WDRUNGE_MODEL_RETRIES', '3')))
    except ValueError:
        return 3


def annotate(response, started, retries):
    """Latency and retry count travel with the response into usage records; the wall
    time of the request lets context memory tell whether the provider cache is cold."""
    metadata = getattr(response, 'response_metadata', None)
    if isinstance(metadata, dict):
        metadata['wdrunge_latency_ms'] = round((time.monotonic() - started) * 1000)
        metadata['wdrunge_retries'] = retries
        metadata['wdrunge_requested_at'] = round(time.time() - (time.monotonic() - started), 3)
    return response


# Provider health across all agents: a burst of transient failures pauses new work.
_failures: deque = deque(maxlen=50)


def note_failure(now=None):
    _failures.append(time.monotonic() if now is None else now)


def provider_cooldown(now=None) -> float:
    """Seconds to wait before starting new work, 0 when the provider looks healthy."""
    now = time.monotonic() if now is None else now
    try:
        threshold = max(2, int(os.getenv('WDRUNGE_PROVIDER_BREAKER', '4')))
    except ValueError:
        threshold = 4
    recent = [t for t in _failures if now - t <= 90]
    if len(recent) < threshold:
        return 0.0
    return max(0.0, recent[-1] + 60 - now)


async def ainvoke_with_retry(runnable, value, *, attempts=None, base_delay=2.0, alternates=(), **kwargs):
    """Retry transient failures on the same call; then try each alternate once (for
    example a non-streaming call, then a fallback model). Never retries other errors."""
    attempts = attempts or retry_attempts()
    last = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            return annotate(await runnable.ainvoke(value, **kwargs), started, attempt - 1)
        except Exception as error:
            if not is_transient(error):
                raise
            note_failure()
            last = error
            if attempt == attempts:
                break
            delay = base_delay * 2 ** (attempt - 1)
            logger.warning('Transient model failure (%s); retrying in %.0fs (%d/%d)', type(error).__name__, delay, attempt, attempts - 1)
            await asyncio.sleep(delay)
    for index, make in enumerate(alternates):
        alternate = make()
        if alternate is None:
            continue
        started = time.monotonic()
        logger.warning('Model call still failing (%s); trying alternate %d', type(last).__name__, index + 1)
        try:
            return annotate(await alternate.ainvoke(value, **kwargs), started, attempts + index)
        except Exception as error:
            if not is_transient(error):
                raise
            note_failure()
            last = error
    raise last
