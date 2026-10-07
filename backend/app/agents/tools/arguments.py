"""Tolerant handling of the arguments a model sends, before a tool validates them.

LangGraph does no fuzzy parsing and LangChain only completes missing closing brackets
on the streaming path. Here, at the one place every tool call passes through (the
worker's tool node and the Leader's loop):

- a call whose arguments are not valid JSON is repaired when that takes only syntax
  (code fences, trailing commas, single quotes, Python literals, bare keys, raw
  newlines in strings, a missing closing bracket). Output cut off by the length limit
  is never repaired: half a file must not be written as if it were whole;
- arguments the tool does not declare are dropped and the model is told, instead of
  the whole call being refused; a name that looks like a declared argument left unset
  is refused with the likely name, because dropping it would silently change the call;
- an array or object sent as a JSON-encoded string is decoded;
- an empty string for an optional argument that may not be empty is taken as "not given".

Type coercions ("true" to a boolean, "3" to an integer) are left to Pydantic's lax
mode. Nothing here guesses a missing required argument or changes what a value means.
"""
from __future__ import annotations

import difflib
import json
import re
import types
from dataclasses import dataclass, field
from typing import Annotated, Union, get_args, get_origin
from uuid import uuid4

from pydantic import BaseModel

TRUNCATED_TEXT = '调用被截断，没有执行；把内容分成几次写。'
NOTICE = re.compile(r'\A\[notice: [^\n]*\]\n')


# --- syntax repair -------------------------------------------------------------

class Truncated(ValueError):
    """The text ends inside a value: the output was cut off, not mistyped."""


LITERALS = {'true': True, 'false': False, 'null': None, 'True': True, 'False': False, 'None': None}
ESCAPES = {'"': '"', "'": "'", '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}
BARE_KEY = re.compile(r'[A-Za-z_][\w-]*')
TOKEN = re.compile(r'[^\s,\]}:]+')


class _Parser:
    def __init__(self, text: str):
        self.text, self.at, self.repairs = text, 0, set()

    def space(self):
        while self.at < len(self.text) and self.text[self.at].isspace():
            self.at += 1

    def value(self):
        self.space()
        if self.at >= len(self.text):
            raise Truncated('the arguments end where a value is expected')
        char = self.text[self.at]
        if char == '{':
            return self.object()
        if char == '[':
            return self.array()
        if char in '"\'':
            return self.string(char)
        token = TOKEN.match(self.text, self.at)
        if not token:
            raise ValueError(f'unexpected {char!r} at character {self.at}')
        self.at = token.end()
        word = token.group()
        if word in LITERALS:
            if word not in ('true', 'false', 'null'):
                self.repairs.add('Python literals')
            return LITERALS[word]
        try:
            return int(word)
        except ValueError:
            try:
                return float(word)
            except ValueError:
                raise ValueError(f'unexpected {word[:40]!r} at character {token.start()}') from None

    def string(self, quote):
        if quote == "'":
            self.repairs.add('single quotes')
        self.at += 1
        parts = []
        while self.at < len(self.text):
            char = self.text[self.at]
            if char == quote:
                self.at += 1
                return ''.join(parts)
            if char == '\\':
                following = self.text[self.at + 1:self.at + 2]
                if not following:
                    break
                if following == 'u' and re.fullmatch(r'[0-9a-fA-F]{4}', self.text[self.at + 2:self.at + 6]):
                    parts.append(chr(int(self.text[self.at + 2:self.at + 6], 16)))
                    self.at += 6
                    continue
                parts.append(ESCAPES.get(following, '\\' + following))
                self.at += 2
                continue
            if char in '\n\r\t':
                self.repairs.add('raw line breaks in a string')
            parts.append(char)
            self.at += 1
        raise Truncated('a string is not finished')

    def members(self, close, item):
        """Items up to `close`; stray commas are skipped and a missing final bracket is closed."""
        self.at += 1
        last_comma, separated = False, True
        while True:
            self.space()
            if self.at >= len(self.text):
                self.repairs.add('missing closing bracket')
                return
            char = self.text[self.at]
            if char == close:
                if last_comma:
                    self.repairs.add('trailing comma')
                self.at += 1
                return
            if char == ',':
                self.at += 1
                last_comma = separated = True
                continue
            if not separated:
                raise ValueError(f'expected "," or "{close}" at character {self.at}')
            last_comma = separated = False
            item()

    def object(self):
        result = {}

        def member():
            char = self.text[self.at]
            if char in '"\'':
                key = self.string(char)
            else:
                bare = BARE_KEY.match(self.text, self.at)
                if not bare:
                    raise ValueError(f'unexpected {char!r} at character {self.at}')
                key, self.at = bare.group(), bare.end()
                self.repairs.add('unquoted keys')
            self.space()
            if self.at >= len(self.text):
                raise Truncated('a key has no value')
            if self.text[self.at] != ':':
                raise ValueError(f'expected ":" after key {key[:40]!r}')
            self.at += 1
            result[key] = self.value()
        self.members('}', member)
        return result

    def array(self):
        result = []
        self.members(']', lambda: result.append(self.value()))
        return result


def repair_json(raw: str) -> tuple[dict | None, list[str], str | None]:
    """(arguments, repairs made, None) or (None, [], why not). Only for text json.loads refused."""
    text = (raw or '').strip()
    repairs = []
    fenced = re.match(r'```[\w-]*\s*\n?(.*?)\n?```', text, re.S)
    if fenced:
        text, repairs = fenced.group(1).strip(), ['code fence']
    start = text.find('{')
    if start < 0:
        return None, [], '修复后不是一个对象' if text else '参数是空的'
    if start > 0:
        repairs.append('text before the JSON')
    parser = _Parser(text)
    parser.at = start
    try:
        value = parser.value()
    except Truncated as error:
        return None, [], f'{TRUNCATED_TEXT}（{error}）'
    except (ValueError, RecursionError) as error:
        return None, [], f'参数不是合法的 JSON：{error}'
    if not isinstance(value, dict):
        return None, [], '修复后不是一个对象'
    if text[parser.at:].strip():
        repairs.append('text after the JSON')
    return value, [*repairs, *sorted(parser.repairs)], None


def screen_response(response):
    """Make every call of a model response answerable. A call the provider could not
    parse is repaired or kept with a recorded problem; when the output hit the length
    limit, every call of the response is marked truncated. The tool loop answers a
    marked call with an error instead of running it."""
    invalid = list(getattr(response, 'invalid_tool_calls', None) or ())
    calls = list(getattr(response, 'tool_calls', None) or ())
    cut = (getattr(response, 'response_metadata', None) or {}).get('finish_reason') == 'length'
    if not invalid and not (cut and calls):
        return response
    problems, repaired = {}, {}
    for call in invalid:
        identifier = call.get('id') or f'call-{uuid4().hex[:12]}'
        raw = call.get('args') if isinstance(call.get('args'), str) else json.dumps(call.get('args'), ensure_ascii=False)
        arguments, repairs, reason = repair_json(raw or '')
        calls.append({'name': call.get('name') or '', 'args': arguments or {}, 'id': identifier, 'type': 'tool_call'})
        if arguments is None:
            problems[identifier] = {'kind': 'truncated' if reason.startswith(TRUNCATED_TEXT) else 'unparsable',
                                    'reason': reason, 'raw': (raw or '')[:300]}
        else:
            repaired[identifier] = repairs
    if cut:
        for call in calls:
            problems[call['id']] = {'kind': 'truncated', 'reason': TRUNCATED_TEXT + '（输出达到了长度上限）'}
    return response.model_copy(update={
        'tool_calls': calls, 'invalid_tool_calls': [],
        'additional_kwargs': {**response.additional_kwargs, 'call_problems': problems, 'call_repairs': repaired}})


def call_problem(message, call_id) -> dict | None:
    return (getattr(message, 'additional_kwargs', None) or {}).get('call_problems', {}).get(call_id)


def problem_failure(problem: dict, tool_name: str) -> dict:
    from app.agents.tools.validation import EXAMPLE_CALLS, failure_result
    if problem['kind'] == 'truncated':
        return failure_result(problem['reason'], 'invalid_arguments', 'truncated_call',
                              '把内容分成几次调用（例如先写文件的前一部分，再用 edits 追加），每次调用的参数短一些。')
    return failure_result(f"{problem['reason']}。调用没有执行。你发来的参数开头：{problem.get('raw', '')}",
                          'invalid_arguments', 'unparsable_arguments',
                          '参数必须是一个 JSON 对象：键和字符串用双引号，不加代码块标记或说明文字。',
                          **({'example_call': EXAMPLE_CALLS[tool_name]} if tool_name in EXAMPLE_CALLS else {}))


# --- structure and unknown arguments -------------------------------------------

@dataclass
class Normalized:
    args: dict
    ignored: list[str] = field(default_factory=list)   # paths of arguments the tool does not declare
    repaired: list[str] = field(default_factory=list)  # structural fixes and coercions, for the call log
    failure: dict | None = None

    # Shape corrections the model should know about (it would repeat the shape otherwise).
    corrected: list[str] = field(default_factory=list)

    def notice(self, tool_name: str) -> str | None:
        parts = ([f"{'、'.join(self.ignored)} 不是 {tool_name} 的参数，已忽略。"] if self.ignored else []) + self.corrected
        return ' '.join(parts) or None

    def notes(self) -> dict | None:
        notes = {key: value for key, value in (('ignored', self.ignored), ('repaired', self.repaired),
                                               ('corrected', self.corrected)) if value}
        return notes or None


def _plain(name: str) -> str:
    return re.sub(r'[_\-\s]', '', name).lower()


def _similar(name: str, candidates) -> str | None:
    """A declared argument the unknown name was probably meant to be."""
    plain = {_plain(c): c for c in candidates}
    if _plain(name) in plain:
        return plain[_plain(name)]
    close = difflib.get_close_matches(_plain(name), list(plain), n=1, cutoff=0.8)
    return plain[close[0]] if close else None


def _unwrap(annotation):
    """The single concrete type of an annotation, or None when it is a real union."""
    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation = get_args(annotation)[0]
        elif origin in (Union, types.UnionType):
            choices = [a for a in get_args(annotation) if a is not type(None)]
            if len(choices) != 1:
                return None
            annotation = choices[0]
        else:
            return annotation


def _needs_text(annotation) -> bool:
    """Whether the annotation is optional text with a minimum length: an empty string
    cannot be a value for it, so a model that sends one means the argument is not given."""
    if get_origin(annotation) not in (Union, types.UnionType) or type(None) not in get_args(annotation):
        return False
    for choice in get_args(annotation):
        if get_origin(choice) is Annotated and get_args(choice)[0] is str:
            for item in get_args(choice)[1:]:
                limits = [getattr(item, 'min_length', None), *(getattr(m, 'min_length', None) for m in getattr(item, 'metadata', ()))]
                if any(isinstance(limit, int) and limit >= 1 for limit in limits):
                    return True
    return False


def _decode(value, kind, path, report):
    """A list or object sent as a JSON-encoded string (a known habit of Qwen)."""
    if isinstance(value, str) and value.lstrip()[:1] in ('[', '{'):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if isinstance(decoded, kind):
            report.repaired.append(f'{path}: JSON string decoded')
            return decoded
    return value


def _walk(annotation, value, path, report):
    annotation = _unwrap(annotation)
    if annotation is None:
        return value
    origin = get_origin(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        value = _decode(value, dict, path, report)
        return _model(annotation, value, path, report) if isinstance(value, dict) else value
    if annotation is list or origin is list:
        value = _decode(value, list, path, report)
        item = (get_args(annotation) or (None,))[0]
        if isinstance(value, list) and item is not None:
            return [_walk(item, element, f'{path}[{index}]', report) for index, element in enumerate(value)]
        return value
    if annotation is dict or origin is dict:
        return _decode(value, dict, path, report)
    if annotation in (bool, int, float) and isinstance(value, str):
        report.repaired.append(f'{path}: string sent for {annotation.__name__}')
    return value


def _model(model: type[BaseModel], value: dict, path: str, report: Normalized) -> dict:
    fields = model.model_fields
    # Names a schema accepts without advertising them (its own before-validator places them).
    accepted = set(fields) | set(getattr(model, 'ACCEPTED_EXTRA', ()))
    result = {}
    for key, item in value.items():
        where = f'{path}.{key}' if path else str(key)
        if key not in accepted:
            unset = [name for name in fields if name not in value]
            intended = _similar(str(key), unset)
            if intended and report.failure is None:
                from app.agents.tools.validation import failure_result
                report.failure = failure_result(
                    f'{where} 不是参数；你是不是想写 {intended}？调用没有执行。', 'invalid_arguments', 'unknown_argument',
                    f'把 {key} 改成 {intended} 再调用一次。')
            report.ignored.append(where)
            continue
        if key in fields and item == '' and not fields[key].is_required() and _needs_text(fields[key].annotation):
            report.repaired.append(f'{where}: empty string taken as not given')
            continue
        result[key] = _walk(fields[key].annotation, item, where, report) if key in fields else item
    return result


# --- command lines --------------------------------------------------------------

# Outside quotes these make a line mean something only a shell does (pipes, redirects,
# sequencing, substitution, globbing, expansion).
SHELL_OPERATORS = set('|&;<>()`$*?[]{}~\n')


def shell_syntax(line: str) -> bool:
    quote = None
    for char in line:
        if quote == "'":
            quote = None if char == "'" else quote
        elif quote == '"':
            if char == '"':
                quote = None
            elif char in '$`':
                return True
        elif char in '\'"':
            quote = char
        elif char in SHELL_OPERATORS:
            return True
    return False


def call_argv(args: dict) -> list[str]:
    """The command of an execute_command call, also from calls recorded before argv
    replaced the separate command and arguments."""
    argv = args.get('argv')
    if isinstance(argv, list):
        return [str(item) for item in argv]
    if isinstance(argv, str):
        return [argv]
    return [str(args.get('command') or ''), *map(str, args.get('arguments') or [])]


def shape_argv(args: dict, report: Normalized, legacy: str | None = None) -> dict:
    """One command as one array, whatever shape the model sent: the separate
    command/arguments fields execute_command used to have (`legacy` names the field an
    older schema used for the whole array), or a plain command line as a string. A line
    with shell syntax is refused rather than guessed at: only a shell gives it meaning."""
    import shlex
    from app.agents.tools.validation import failure_result
    if legacy and 'argv' not in args and legacy not in args and isinstance(args.get('arguments'), list):
        # The whole command sent as the old arguments field.
        args['argv'] = args.pop('arguments')
        report.corrected.append('arguments 已当作 argv：命令只用 argv 一个数组表示，可执行文件是第一个元素。')
    if 'argv' not in args and legacy in args:
        command, rest = args.pop(legacy), args.pop('arguments', None)
        rest = _decode(rest, list, 'arguments', report)
        if rest is not None and not isinstance(rest, list):
            report.failure = failure_result(
                'arguments 不是数组（也不是合法的 JSON 数组），调用没有执行。', 'invalid_arguments', 'invalid_argv',
                '命令只用 argv 一个数组表示，每个元素是带引号的字符串：{"argv": ["grep", "-n", "pattern", "path"]}。')
            return args
        if isinstance(command, list):
            args['argv'] = [*command, *(rest if isinstance(rest, list) else [])]
        elif isinstance(rest, list):
            # The old split invited repeating the executable as the first argument.
            args['argv'] = [command, *(rest[1:] if rest[:1] == [command] else rest)]
        else:
            args['argv'] = command
        report.corrected.append(f'{legacy}' + ('/arguments' if rest is not None else '') + ' 已合并成 argv：命令只用 argv 一个数组表示，可执行文件是第一个元素。')
    argv = _decode(args.get('argv'), list, 'argv', report)
    def split(line):
        if shell_syntax(line):
            report.failure = failure_result(
                f'argv 是一行带 shell 语法的命令（{line[:200]}），调用没有执行。', 'invalid_arguments', 'shell_command_line',
                'argv 是数组：["uv", "sync"]。需要管道、重定向或 && 时，整行交给 shell：["sh", "-c", "<整行命令>"]。')
            return None
        try:
            return shlex.split(line)
        except ValueError:
            return None
    if isinstance(argv, str):
        parts = split(argv)
        if parts:
            report.corrected.append(f'argv 是字符串，已按 shell 规则拆成数组 {json.dumps(parts, ensure_ascii=False)[:200]}；下次直接发数组。')
            argv = parts
    elif isinstance(argv, list) and argv and isinstance(argv[0], str) and ' ' in argv[0].strip() and not _existing(argv[0], args):
        parts = split(argv[0])
        if parts:
            report.corrected.append(f'argv[0] 是整行命令，已拆开：可执行文件 {parts[0]!r}，其余是参数；下次每个参数单独一个元素。')
            argv = [*parts, *argv[1:]]
    if argv is not None:
        args['argv'] = argv
    return args


def _existing(path: str, args: dict) -> bool:
    """Whether a first element with spaces is a real path (a project in "Agent Test Workspace")."""
    from pathlib import Path
    from app.agents.tools._workspace import workspace_root
    candidate = Path(path) if Path(path).is_absolute() else workspace_root() / str(args.get('cwd') or '.') / path
    try:
        return candidate.exists()
    except OSError:
        return False


# Tool-specific shape repairs, run before the generic ones below.
SHAPERS = {
    'execute_command': lambda args, report: shape_argv(args, report, 'command'),
    'collaboration_register_command': lambda args, report: shape_argv(args, report),
    'collaboration_register_service': lambda args, report: shape_argv(args, report, 'command') if args.get('command') or args.get('argv') else args,
}


def normalize_arguments(tool, arguments) -> Normalized:
    """Arguments ready for the tool's own validation, with what was dropped or fixed."""
    report = Normalized(arguments if isinstance(arguments, dict) else {})
    schema = getattr(tool, 'args_schema', None)
    if not isinstance(arguments, dict) or not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        return report
    if shaper := SHAPERS.get(getattr(tool, 'name', '')):
        arguments = shaper(dict(arguments), report)
        if report.failure:
            return report
    report.args = _model(schema, arguments, '', report)
    return report


def with_notice(content, notice: str | None):
    """Put the notice first in a result: a key of a JSON object, else a leading line
    (strip_notice removes it where a result's text is parsed)."""
    if not notice:
        return content
    if isinstance(content, dict):
        return {'notice': notice, **content}
    if isinstance(content, str):
        try:
            data = json.loads(content)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return json.dumps({'notice': notice, **data}, ensure_ascii=False)
        return f'[notice: {notice}]\n{content}'
    return content


def strip_notice(text: str) -> str:
    return NOTICE.sub('', text, count=1) if isinstance(text, str) else text
