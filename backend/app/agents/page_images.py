"""Page screenshots shown to the model with the browser_verify result that took them.

The result in the history stays JSON text (folding, journals, recall and the ledger
parse it). The screenshots are attached only to the messages of one model call, read
from the workspace, for the latest result while it is unfolded and for
WDRUNGE_PAGE_IMAGE_TURNS replies after it (0 disables, e.g. for a text-only model).
The model then reviews layout, styling and content by looking at the page, instead of
re-running the check with more text assertions.
"""
from __future__ import annotations

import base64
import json
import math
import os
import struct
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.agents.context_fold import FOLDED_TEXT, facts_of
from app.agents.tools._workspace import workspace_root

# The provider scales an image above this many pixels down to it; then a token is
# 32x32 pixels (measured on qwen3.8-max: 1440x1000 → 1,410 tokens, any size above
# 1440x1820 → about 2,560).
MAX_PIXELS = 2_621_440
PIXELS_PER_TOKEN = 1024
NOTE = ('Screenshot of the page above (full page, top to bottom{parts}). Review layout, styling and content '
        'from it; text assertions are for exact strings and behavior.')


def image_turns() -> int:
    raw = os.getenv('WDRUNGE_PAGE_IMAGE_TURNS', '').strip()
    return int(raw) if raw.isdigit() else 3


def _png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        return None
    return struct.unpack('>II', data[16:24])


def image_tokens(width: int, height: int) -> int:
    return math.ceil(min(width * height, MAX_PIXELS) / PIXELS_PER_TOKEN) + 4


def _screenshots(message: ToolMessage) -> list[Path]:
    """Existing workspace PNGs named by an unfolded browser_verify result."""
    if FOLDED_TEXT.search(message.content if isinstance(message.content, str) else '') or facts_of(message).get('level'):
        return []
    try:
        data = json.loads(message.content)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    names = data.get('screenshots') or ([data['screenshot']] if data.get('screenshot') else [])
    root = workspace_root().resolve()
    paths = []
    for name in names:
        if not isinstance(name, str) or not name.endswith('.png'):
            continue
        path = (root / name).resolve()
        if path.is_relative_to(root) and path.is_file():
            paths.append(path)
    return paths


def _latest(messages: list[BaseMessage]) -> tuple[int, list[Path]] | None:
    """The latest browser_verify result, if fewer than image_turns() replies follow it."""
    turns = image_turns()
    if turns <= 0:
        return None
    replies = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AIMessage):
            replies += 1
            if replies >= turns:
                return None
        elif isinstance(message, ToolMessage) and message.name == 'browser_verify':
            paths = _screenshots(message)
            return (index, paths) if paths else None
    return None


def pending_tokens(messages: list[BaseMessage]) -> int:
    """Actual tokens the attached screenshots will add to the next call."""
    found = _latest(messages)
    if not found:
        return 0
    total = 0
    for path in found[1]:
        with path.open('rb') as stream:
            size = _png_size(stream.read(24))
        total += image_tokens(*size) if size else image_tokens(1440, 1820)
    return total


def with_page_images(messages: list[BaseMessage]) -> tuple[list[BaseMessage], int]:
    """The messages for one model call, with the screenshots attached to the latest
    browser_verify result; and the actual tokens they add (for calibration)."""
    found = _latest(messages)
    if not found:
        return messages, 0
    index, paths = found
    parts, tokens = [], 0
    for path in paths:
        data = path.read_bytes()
        size = _png_size(data)
        tokens += image_tokens(*size) if size else image_tokens(1440, 1820)
        parts.append({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(data).decode()}})
    note = NOTE.format(parts=f', {len(paths)} consecutive parts' if len(paths) > 1 else '')
    shown = messages[index].model_copy(update={'content': [
        {'type': 'text', 'text': messages[index].content}, {'type': 'text', 'text': note}, *parts]})
    return [*messages[:index], shown, *messages[index + 1:]], tokens
