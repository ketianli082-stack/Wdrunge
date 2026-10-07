"""Calibrating the token estimate against what the provider actually charged.

Every threshold of context memory is configured in real (billed) tokens. The estimate
(UTF-8 bytes / 2, app.agents.context_fold.estimate) overstates Chinese and code by
about 1.5x (Blog4: actual ≈ 0.643 × estimate), so a thread keeps a ratio
actual / estimated, measured from each call's reported prompt_tokens, and the layers
convert their limits into estimated units with it. The ratio travels in the
response_metadata of the model's replies, so it is persisted with the history.
"""
from __future__ import annotations

import json
import math

from langchain_core.messages import AIMessage, BaseMessage

from app.agents.context_fold import estimate, text_of

RATIO_KEY = 'wdrunge_token_ratio'
DEFAULT, LOW, HIGH = 1.0, 0.4, 1.2
SMOOTHING = 0.3
# Below this many estimated tokens a measurement says little (fixed overhead dominates).
MINIMUM_SAMPLE = 2000


def estimated_tokens(messages: list[BaseMessage]) -> int:
    return sum(12 + estimate(text_of(m)) + (estimate(json.dumps(m.tool_calls, ensure_ascii=False))
                                            if isinstance(m, AIMessage) and m.tool_calls else 0) for m in messages)


def recorded_ratio(messages: list[BaseMessage]) -> float | None:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            value = (message.response_metadata or {}).get(RATIO_KEY)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
    return None


def ratio_of(messages: list[BaseMessage]) -> float:
    """actual tokens per estimated token for this history (1.0 until calibrated)."""
    return recorded_ratio(messages) or DEFAULT


def prompt_tokens(response) -> int | None:
    usage = getattr(response, 'usage_metadata', None) or {}
    metadata = getattr(response, 'response_metadata', None) or {}
    fallback = metadata.get('token_usage') if isinstance(metadata, dict) else None
    value = (usage.get('input_tokens') if isinstance(usage, dict) else None)
    if value is None and isinstance(fallback, dict):
        value = fallback.get('prompt_tokens')
    return value if isinstance(value, int) and value > 0 else None


def calibrate(sent: list[BaseMessage], response, schema_tokens: int = 0, media_tokens: int = 0) -> float | None:
    """Record on `response` the ratio measured from this call (smoothed with the earlier
    value), or None when the provider reported no usage or the sample is too small.
    media_tokens: actual tokens of images attached to `sent` for this call only
    (app.agents.page_images); they are not text, so they are left out of the ratio."""
    actual = prompt_tokens(response)
    metadata = getattr(response, 'response_metadata', None)
    if actual is None or not isinstance(metadata, dict):
        return None
    actual -= media_tokens
    estimated = estimated_tokens(sent) + schema_tokens
    if estimated < MINIMUM_SAMPLE:
        return None
    measured = actual / estimated
    previous = recorded_ratio(sent)
    ratio = measured if previous is None else previous * (1 - SMOOTHING) + measured * SMOOTHING
    ratio = round(min(HIGH, max(LOW, ratio)), 3)
    metadata[RATIO_KEY] = ratio
    return ratio


def in_estimate(actual: float, ratio: float) -> int:
    """A limit given in actual tokens, in estimated units."""
    return math.ceil(actual / max(ratio, LOW))
