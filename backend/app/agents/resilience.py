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


def annotate(response, started, retries, errors=None):
    """Latency, retry count and what failed travel with the response into usage records;
    the wall time of the request lets context memory tell whether the provider cache is
    cold."""
    metadata = getattr(response, 'response_metadata', None)
    if isinstance(metadata, dict):
        metadata['wdrunge_latency_ms'] = round((time.monotonic() - started) * 1000)
        metadata['wdrunge_retries'] = retries
        metadata['wdrunge_requested_at'] = round(time.time() - (time.monotonic() - started), 3)
        if errors:
            metadata['wdrunge_retry_errors'] = errors
    return response


def error_name(error: BaseException) -> str:
    """The failure as a run analysis needs it: the class, and the HTTP status if any."""
    status = getattr(error, 'status_code', None)
    return type(error).__name__ + (f' {status}' if isinstance(status, int) else '')


def error_summary(names) -> str:
    """'APIConnectionError x3, RateLimitError 429' from the failures of one call."""
    counts = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return ', '.join(name if count == 1 else f'{name} x{count}' for name, count in counts.items())[:200]


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


def route_of(runnable):
    """The provider path a runnable calls: its model name, looked up through tool
    bindings. None for anything else (a test double), which is then never rerouted."""
    for _ in range(5):
        name = getattr(runnable, 'model_name', None)
        if isinstance(name, str) and name:
            return name
        runnable = getattr(runnable, 'bound', None)
        if runnable is None:
            break
    return None


# Routes whose normal call keeps failing while an alternate answers:
# route -> (index of that alternate, when the normal call is tried again, what failed).
_preferred: dict = {}


def alternate_hold() -> float:
    """Seconds an alternate that answered goes first before the normal call is tried
    again; 0 turns this off."""
    try:
        return max(0.0, float(os.getenv('WDRUNGE_ALTERNATE_HOLD', '120')))
    except ValueError:
        return 120.0


async def ainvoke_with_retry(runnable, value, *, attempts=None, base_delay=2.0, alternates=(), route=None, **kwargs):
    """Retry transient failures on the same call; then try each alternate once (for
    example a non-streaming call, then a fallback model). Never retries other errors.

    route names the provider path the calls share (the model). When the normal call has
    used up its attempts and an alternate answered, the fault is in that path, not in
    this one request: for alternate_hold() seconds calls on the route start with that
    alternate, and afterwards the normal call gets one attempt before its retries are
    spent again. (Blog11: 13 calls in a row each failed three streamed attempts within
    seconds and were answered without streaming, about 10 s lost on every one.)"""
    attempts = attempts or retry_attempts()
    last, failed = None, []
    held = _preferred.get(route) if route is not None else None

    def note(error):
        nonlocal last
        note_failure()
        last = error
        failed.append(error_name(error))

    async def call(target, retries):
        started = time.monotonic()
        return annotate(await target.ainvoke(value, **kwargs), started, retries, error_summary(failed))

    tried = None
    if held and time.monotonic() < held[1]:
        index, _, reason = held
        alternate = alternates[index]() if index < len(alternates) else None
        if alternate is not None:
            tried = index
            try:
                response = await call(alternate, 0)
            except Exception as error:
                if not is_transient(error):
                    raise
                note(error)
                logger.warning('Preferred alternate %d failed (%s); back to the normal call', index + 1, failed[-1])
            else:
                metadata = getattr(response, 'response_metadata', None)
                if isinstance(metadata, dict):
                    metadata['wdrunge_retry_errors'] = f'alternate {index + 1} first after {reason}'[:200]
                return response
        _preferred.pop(route, None)
        held = None
    if held:
        attempts = 1  # the hold has run out: one look at whether the normal path is back
    for attempt in range(1, attempts + 1):
        try:
            response = await call(runnable, attempt - 1)
        except Exception as error:
            if not is_transient(error):
                raise
            note(error)
            if attempt == attempts:
                break
            delay = base_delay * 2 ** (attempt - 1)
            logger.warning('Transient model failure (%s); retrying in %.0fs (%d/%d)', failed[-1], delay, attempt, attempts - 1)
            await asyncio.sleep(delay)
        else:
            if held:
                _preferred.pop(route, None)
            return response
    for index, make in enumerate(alternates):
        if index == tried:
            continue  # it failed a moment ago in this same call
        alternate = make()
        if alternate is None:
            continue
        logger.warning('Model call still failing (%s); trying alternate %d', failed[-1], index + 1)
        before = error_summary(failed)
        try:
            response = await call(alternate, attempts + index)
        except Exception as error:
            if not is_transient(error):
                raise
            note(error)
            continue
        hold = alternate_hold()
        if route is not None and hold:
            _preferred[route] = (index, time.monotonic() + hold, before)
        return response
    raise last
