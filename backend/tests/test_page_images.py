"""Page screenshots ride along on model calls after a browser check, and only then."""
import json
import struct
import zlib

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents import context_fold
from app.agents.context_meter import calibrate, estimated_tokens
from app.agents.page_images import image_tokens, pending_tokens, with_page_images
from app.agents.tools._workspace import active_workspace


def png(path, width, height):
    raw = b''.join(b'\x00' + b'\xff' * (3 * width) for _ in range(height))
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def check(name, screenshot='docs/testing/browser-a.png', **extra):
    call = AIMessage(content='', id=f'ai-{name}', tool_calls=[{'id': name, 'name': 'browser_verify', 'args': {'url': 'http://localhost:5173/'}}])
    result = ToolMessage(content=json.dumps({'url': 'http://localhost:5173/', 'passed': True, 'screenshot': screenshot, **extra}),
                         name='browser_verify', tool_call_id=name, id=f'r-{name}')
    return [call, result]


def reply(n):
    return AIMessage(content='', id=f'ai-x{n}', tool_calls=[{'id': f'x{n}', 'name': 'read_project_file', 'args': {'path': 'a.vue'}}])


def test_latest_result_carries_its_screenshot_for_a_few_calls(tmp_path, monkeypatch):
    token = active_workspace.set(tmp_path)
    try:
        png(tmp_path / 'docs/testing/browser-a.png', 1440, 1000)
        png(tmp_path / 'docs/testing/browser-b.png', 1440, 3000)
        history = [HumanMessage(content='verify the page'), *check('a')]
        shown, tokens = with_page_images(history)
        assert tokens == image_tokens(1440, 1000) == pending_tokens(history)
        parts = shown[-1].content
        assert parts[0] == {'type': 'text', 'text': history[-1].content} and parts[2]['image_url']['url'].startswith('data:image/png;base64,')
        assert history[-1].content.startswith('{')  # the history itself stays text
        # Shown for the call right after the result and two more; then dropped.
        later = [*history, reply(1), ToolMessage(content='x', tool_call_id='x1'), reply(2), ToolMessage(content='x', tool_call_id='x2')]
        assert with_page_images(later)[1] > 0
        assert with_page_images([*later, reply(3), ToolMessage(content='x', tool_call_id='x3')]) == ([*later, reply(3), ToolMessage(content='x', tool_call_id='x3')], 0)
        monkeypatch.setenv('WDRUNGE_PAGE_IMAGE_TURNS', '0')
        assert with_page_images(history) == (history, 0)
        monkeypatch.delenv('WDRUNGE_PAGE_IMAGE_TURNS')
        # A newer check replaces the older screenshot; a tall image costs at most the provider cap.
        newer = [*history, *check('b', 'docs/testing/browser-b.png')]
        shown, tokens = with_page_images(newer)
        assert isinstance(shown[2].content, str) and isinstance(shown[-1].content, list)
        assert tokens == image_tokens(1440, 3000) <= 2570
    finally:
        active_workspace.reset(token)


def test_folded_missing_or_outside_screenshots_are_not_attached(tmp_path):
    token = active_workspace.set(tmp_path)
    try:
        png(tmp_path / 'docs/testing/browser-a.png', 100, 100)
        png(tmp_path.parent / 'outside.png', 100, 100)
        history = check('a')
        folded = history[1].model_copy(update={'content': history[1].content + '\n[Folded browser_verify result; recall_context(message_id="r-a") returns the original.]'})
        assert with_page_images([history[0], folded])[1] == 0
        stubbed = context_fold.with_facts(history[1], context_fold.STUB)
        assert with_page_images([history[0], stubbed])[1] == 0
        assert with_page_images(check('m', 'docs/testing/missing.png'))[1] == 0
        assert with_page_images(check('o', '../outside.png'))[1] == 0
    finally:
        active_workspace.reset(token)


def test_image_tokens_are_left_out_of_the_text_calibration():
    sent = [HumanMessage(content='x' * 20000)]
    estimated = estimated_tokens(sent)
    response = AIMessage(content='ok', usage_metadata={'input_tokens': 5000 + 2560, 'output_tokens': 1, 'total_tokens': 7561})
    assert calibrate(sent, response, 0, media_tokens=2560) == round(5000 / estimated, 3)


def test_attached_screenshots_are_reserved_in_the_history_budget(tmp_path):
    import asyncio
    from app.agents.base import Agent, AgentSpec
    from app.agents.memory import ContextMemory
    token = active_workspace.set(tmp_path)
    try:
        png(tmp_path / 'docs/testing/browser-a.png', 1440, 3000)
        memory = ContextMemory(Agent(AgentSpec(id='frontend', name='Frontend', prompt_file='frontend.md')), 'images')
        plain = memory.budget
        asyncio.run(memory.prepare([HumanMessage(content='verify', id='t'), *check('a')]))
        assert memory.reserved == image_tokens(1440, 3000)
        assert memory.budget == plain - memory.in_estimate(memory.reserved)
    finally:
        active_workspace.reset(token)
