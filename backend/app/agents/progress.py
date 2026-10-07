"""Read-only streaks: model turns that only read, by what their tool calls do.

A turn is read-only when every tool call in it only reads or inspects (files,
documents, search, directory listings, recalled context, process logs, and shell
commands made only of read utilities). Writing, publishing, delivering, running a
build, test or service and browser checks all move work forward, and so does a journal
note (a stated plan). After many read-only turns in a row the runtime reminds the agent
to produce output; after twice as many, reads are paused (refused with an explanation)
until the agent writes, runs something or records its next step (see app.agents.worker).
The context is kept: a long read run usually means the working set is large, and
clearing it only made the agent read the same files again.
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath
import shlex

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.agents.tools.arguments import call_argv

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


# Shell grammar around commands: grouping, and keywords that open or close a loop or
# condition (the command after `do`, `then`, ... is judged on its own).
GROUPING = {'(', ')', '{', '}'}
OPENERS = {'do', 'then', 'else', 'elif', 'if', 'while', 'until', '!'}
CLOSERS = {'done', 'fi'}
HARMLESS = {'cd', 'printf', 'true', 'test', '['}
# Redirection targets that write nothing: discarding output or merging streams.
DISCARD = {'/dev/null'}


def read_only_script(script: str) -> bool:
    """A shell script made only of read utilities, writing nothing (output may go to
    /dev/null or be merged into stdout). Quoted text is data: `awk 'NR>=460'` reads."""
    if '`' in script or '$(' in script:
        return False
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    command: list[str] = []
    index = 0
    while index <= len(tokens):
        token = tokens[index] if index < len(tokens) else ';'
        if token in SEPARATORS or token in GROUPING or set(token) <= set(';&|') or token == '\n':
            if command and not read_only_command(command):
                return False
            command = []
        elif '>' in token and set(token) <= set('<>&|'):
            target = tokens[index + 1] if index + 1 < len(tokens) else ''
            if not (target in DISCARD or token.endswith('&') and target.isdigit()):
                return False
            if command and command[-1].isdigit():
                command.pop()  # the file descriptor of `2>&1`
            index += 1
        elif token.startswith('<'):
            index += 1  # reading input from a file or here-string
        else:
            command.append(token)
        index += 1
    return True


def read_only_command(command: list[str]) -> bool:
    while command and command[0] in OPENERS:
        command = command[1:]
    if not command or command[0] in CLOSERS:
        return True
    if command[0] == 'for':
        return True  # `for name in words`: the body follows `do`
    if PurePosixPath(command[0]).name in HARMLESS:
        return True
    return read_only_argv(command)


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
        return read_only_argv(call_argv(args))
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


PAUSE_TEXT = ('读取已暂停：你已连续 {count} 轮只做读取。用已知信息推进：写出下一个文件或下一段（可以先写骨架），'
              '或运行一次检查；不确定下一步时，先用 collaboration_journal(kind="next") 写下要写什么、'
              '已确认的名称和签名、还缺哪一个具体事实。任何写入、运行或 journal 记录之后读取立即恢复。'
              '任务台账的代码索引列出了你写过或整读过的文件里的声明和行号。')


def reads_paused(history: list[BaseMessage]) -> int:
    """The read-only streak before the newest response when reads are paused, else 0."""
    streak = read_streak(history)
    return streak if streak >= 2 * read_streak_limit() else 0


def streak_notice(count: int, paused: bool) -> HumanMessage:
    text = (f'平台提示：你已连续 {count} 轮只做读取（读文件、读文档、搜索、查看），没有写入、发布或运行。'
            '现在用已有信息写出下一个文件或下一节（可以先写骨架），缺的细节写完后再按需读取单个章节或片段；'
            '不要为了一次写完而继续读全部材料。确实被阻塞时用 collaboration_journal 记录 blocked 并说明原因。')
    if paused:
        text = PAUSE_TEXT.format(count=count)
    else:
        text += f'连续 {2 * read_streak_limit()} 轮只读后，读取会暂停，直到你写入、运行或用 journal 写下下一步。'
    # A reminder, so summaries drop it and it is never taken for the task input.
    return HumanMessage(content=text, additional_kwargs={STREAK_KEY: count, 'reissuable_reminder': True})
