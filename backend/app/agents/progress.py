"""Read-only streaks: model turns that only read, by what their tool calls do.

A turn is read-only when every tool call in it only reads or inspects (files,
documents, search, directory listings, recalled context, process logs, and shell
commands made only of read utilities). Writing, publishing, delivering, running a
build, test or service and browser checks all move work forward. After many read-only
turns in a row the runtime reminds the agent to produce output, and after twice as many
it continues the task in a clean platform handoff (see app.agents.worker).
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath
import shlex

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

READ_TOOLS = {
    'read_project_file', 'browse_directory', 'search_code', 'list_project_files', 'detect_environment',
    'recall_context', 'current_utc_time', 'read_external_file', 'enable_tools', 'get_goal',
}
READ_ACTIONS = {
    'catalog', 'read', 'search', 'diff', 'history', 'snapshot', 'dependencies', 'inspect_request',
    'inspect_message', 'handoff', 'environment', 'maintenance', 'check_delivery',
}
READ_COMMANDS = {
    'cat', 'head', 'tail', 'sed', 'grep', 'egrep', 'fgrep', 'rg', 'ls', 'find', 'wc', 'awk', 'tree', 'file',
    'stat', 'du', 'nl', 'cut', 'sort', 'uniq', 'diff', 'jq', 'pwd', 'echo', 'less', 'more', 'readlink', 'realpath',
    'basename', 'dirname', 'which', 'od', 'hexdump', 'xxd', 'strings', 'column', 'tr',
}
READ_GIT = {'status', 'log', 'diff', 'show', 'grep', 'ls-files', 'blame', 'rev-parse', 'branch'}
SHELLS = {'sh', 'bash', 'zsh'}
SEPARATORS = {'&&', '||', ';', '|', '&'}
STREAK_KEY = 'read_streak'


def read_streak_limit() -> int:
    try:
        return max(3, int(os.getenv('WDRUNGE_READ_STREAK', '12')))
    except ValueError:
        return 12


def read_only_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    name = PurePosixPath(str(argv[0])).name
    args = [str(a) for a in argv[1:]]
    if name in SHELLS:
        if len(args) >= 2 and args[0] in {'-c', '-lc'}:
            return read_only_script(args[1])
        return False
    if name == 'git':
        return bool(args) and args[0] in READ_GIT
    if name not in READ_COMMANDS:
        return False
    if name == 'sed' and any(a == '-i' or a.startswith(('-i', '--in-place')) for a in args):
        return False
    if name == 'find' and any(a in {'-delete', '-exec', '-execdir', '-ok', '-okdir', '-fprint', '-fprintf'} for a in args):
        return False
    return True


def read_only_script(script: str) -> bool:
    """A shell script made only of read utilities, without output redirection."""
    if '>' in script or '`' in script or '$(' in script:
        return False
    try:
        tokens = shlex.split(script, posix=True)
    except ValueError:
        return False
    command: list[str] = []
    for token in [*tokens, ';']:
        if token in SEPARATORS or token.endswith(';'):
            if token.endswith(';') and token != ';':
                command.append(token[:-1])
            if command and not read_only_argv(command):
                return False
            command = []
        else:
            command.append(token)
    return True


def read_only_call(call: dict) -> bool:
    name, args = call.get('name') or '', call.get('args') or {}
    if name in READ_TOOLS:
        return True
    if name == 'collaborate':
        return args.get('action') in READ_ACTIONS
    if name.startswith('collaboration_'):
        return name.removeprefix('collaboration_') in READ_ACTIONS
    if name == 'manage_process':
        return args.get('action') in {'list', 'status', 'logs'}
    if name == 'execute_command' and not args.get('background'):
        return read_only_argv([args.get('command') or '', *(args.get('arguments') or [])])
    return False


def read_only_turn(message: AIMessage) -> bool:
    return bool(message.tool_calls) and all(read_only_call(call) for call in message.tool_calls)


def read_streak(messages: list[BaseMessage]) -> int:
    """Consecutive read-only turns at the end of the history. A streak notice carries the
    count at the time it was given, so the count continues across a clean handoff."""
    count = 0
    for message in reversed(messages):
        if isinstance(message, HumanMessage) and STREAK_KEY in message.additional_kwargs:
            return count + int(message.additional_kwargs[STREAK_KEY])
        if isinstance(message, AIMessage):
            if not read_only_turn(message):
                return count
            count += 1
    return count


def streak_notice(count: int, handoff: bool) -> HumanMessage:
    text = (f'平台提示：你已连续 {count} 轮只做读取（读文件、读文档、搜索、查看），没有写入、发布或运行。'
            '现在用已有信息写出下一个文件或下一节（可以先写骨架），缺的细节写完后再按需读取单个章节或片段；'
            '不要为了一次写完而继续读全部材料。确实被阻塞时用 collaboration_journal 记录 blocked 并说明原因。')
    if handoff:
        text += '平台已把上下文换成干净交接（最近的原文仍保留），下一步必须写入或发布。'
    # A reminder, so summaries drop it and it is never taken for the task input.
    return HumanMessage(content=text, additional_kwargs={STREAK_KEY: count, 'reissuable_reminder': True})
