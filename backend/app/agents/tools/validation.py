"""Tool schemas, what a model is shown of a result, and recoverable tool errors."""
import json
import logging
from langchain_core.tools import tool as langchain_tool
from pydantic import ConfigDict
from pydantic import ValidationError
from langgraph.prebuilt.tool_node import ToolInvocationError

logger = logging.getLogger(__name__)
_schemas = {}
# A minimal valid call per tool, shown only when its arguments fail validation (so it
# costs nothing on the calls that succeed). Tools register their own.
EXAMPLE_CALLS: dict[str, dict] = {}


# What the model is shown of a tool's successful result, by tool name; the tool-call
# log and the UI keep the full result.
_views = {}


def tool(*args, context_policy=None, model_view=None, **kwargs):
    """A tool with a closed schema. Types are validated in Pydantic's lax mode ("true"
    is a boolean, "3" an integer): strict mode refused what LangChain itself accepts,
    and each refusal cost a model turn. Undeclared arguments never reach a tool:
    app.agents.tools.arguments drops them and tells the model; the schema still forbids
    them for callers that skip that step.
    context_policy declares how results may be compacted (app.agents.context_policy;
    undeclared tools are durable); model_view(result, arguments) returns what the model
    is shown of a successful result."""
    def closed(result):
        config = {key: value for key, value in result.args_schema.model_config.items() if key != 'strict'}
        result.args_schema.model_config = ConfigDict(**{**config, 'extra': 'forbid'})
        result.args_schema.model_rebuild(force=True)
        _schemas[result.name] = result.args_schema
        if context_policy:
            from app.agents.context_policy import declare
            declare(result, context_policy)
        if model_view:
            _views[result.name] = model_view
        return result
    result = langchain_tool(*args, **kwargs)
    if hasattr(result, 'args_schema'):
        return closed(result)
    return lambda fn: closed(result(fn))


def model_result(name: str, arguments, content):
    """A tool result as the model sees it: the fields its next step can use. Failures and
    results of tools that declare no view pass unchanged."""
    view = _views.get(name)
    if view is None or not isinstance(content, str):
        return content
    try:
        data = json.loads(content)
    except ValueError:
        data = content
    if not isinstance(data, (dict, str)) or (isinstance(data, dict) and (data.get('error') or data.get('passed') is False)):
        return content
    try:
        shown = view(data, arguments if isinstance(arguments, dict) else {})
    except Exception:
        # A view must never cost a result: a shape it does not expect is shown whole.
        logger.warning('Model view of %s failed; showing the whole result', name, exc_info=True)
        return content
    return shown if isinstance(shown, str) else json.dumps(shown, ensure_ascii=False)


def present(data: dict, *keys) -> dict:
    """The named fields that carry information: absent, None, False and empty ones are left out."""
    return {key: data[key] for key in keys
            if not (data.get(key) is None or data[key] is False or data[key] == '' or data[key] == [] or data[key] == {})}


def _collapse_optional_null(schema):
    if isinstance(schema, list):
        return [_collapse_optional_null(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    schema = {key: _collapse_optional_null(value) for key, value in schema.items()}
    required = set(schema.get('required', ()))
    for name, field in schema.get('properties', {}).items():
        choices = field.get('anyOf', ())
        concrete = [choice for choice in choices if choice.get('type') != 'null']
        if name not in required and len(choices) == 2 and len(concrete) == 1 and field.get('default', None) is None:
            schema['properties'][name] = {**{k: v for k, v in field.items() if k not in {'anyOf', 'default'}}, **concrete[0]}
    return schema


def _strip_titles(schema, in_properties=False):
    """Drop JSON-schema `title` keywords (generated from names, resent on every call);
    keep properties that are themselves named `title`."""
    if isinstance(schema, list):
        return [_strip_titles(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    return {key: _strip_titles(value, key == 'properties') for key, value in schema.items()
            if in_properties or key != 'title'}


def model_tool_schemas(tools) -> list[dict]:
    """OpenAI tool schemas for model binding. Optional fields are advertised by
    their concrete type only: with `anyOf: [array, null]` Qwen sends arrays as
    JSON-encoded strings. Omission still means None; validation stays strict."""
    from langchain_core.utils.function_calling import convert_to_openai_tool
    schemas = []
    for t in tools:
        schema = _collapse_optional_null(convert_to_openai_tool(t))
        schema['function']['parameters'] = _strip_titles(schema['function'].get('parameters', {}))
        # Fields the runtime requires from a model although API callers may omit them.
        fields = getattr(getattr(t, 'args_schema', None), 'model_fields', None) or {}
        demanded = [name for name, field in fields.items() if (getattr(field, 'json_schema_extra', None) or {}).get('model_required')]
        if demanded:
            parameters = schema['function']['parameters']
            parameters['required'] = [*parameters.get('required', ()), *(n for n in demanded if n not in parameters.get('required', ()))]
        schemas.append(schema)
    return schemas


def validation_error_message(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_input=False)
    lines = [f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in errors[:8]]
    if len(errors) > 8:
        lines.append(f'{len(errors) - 8} additional validation errors omitted')
    return 'Invalid arguments: ' + '; '.join(lines)[:2200]


def failure_result(message: str, category: str, code: str, next_action: str = '',
                   retry_policy: str | None = None, **details) -> dict:
    """The error, its category and code, and a next action only when there is a specific
    one: advice that is the same for every failure of a kind was resent with each of
    them and said nothing. retry_policy is accepted for older callers and not returned."""
    return {'error': message[:2500], 'error_details': {
        'category': category, 'code': code, **({'next_action': next_action} if next_action else {}),
        **{key: value for key, value in details.items() if value not in (None, '', [], {})},
    }}


def folded_call_failure(args) -> dict | None:
    """Refuse a call that copies a placeholder from folded history in place of a value
    (it would otherwise be written into files or documents as text)."""
    from app.agents.context_fold import folded_argument
    if (where := folded_argument(args)) is None:
        return None
    return failure_result(f'{where} is a placeholder left by context folding, not the real value; nothing was done.',
        'invalid_arguments', 'folded_placeholder',
        'Send the actual value. To build on earlier text, read the file or recall_context the original first.',
        'correct_arguments')


class ToolFailure(ValueError):
    """An expected operation rejection with an explicit recovery category."""
    def __init__(self, message, category, code, next_action, retry_policy='inspect_before_retry'):
        super().__init__(message)
        self.result = failure_result(message, category, code, next_action, retry_policy)


def _resolve(schema, root):
    if '$ref' in schema:
        return {**{k: v for k, v in schema.items() if k != '$ref'},
                **root.get('$defs', {}).get(schema['$ref'].rsplit('/', 1)[-1], {})}
    choices = schema.get('anyOf', schema.get('oneOf', []))
    return {**{k: v for k, v in schema.items() if k not in {'anyOf', 'oneOf'}},
            **_resolve(next((s for s in choices if s.get('type') != 'null'), {}), root)} if choices else schema


def _field_schema(root, location):
    schema = root
    for part in location:
        schema = _resolve(schema, root)
        schema = schema.get('items', {}) if isinstance(part, int) else schema.get('properties', {}).get(part, {})
    return _resolve(schema, root)


def _shape(schema, root, depth=0):
    schema = _resolve(schema, root)
    result = {k: schema[k] for k in ('type', 'enum', 'const', 'minItems', 'maxItems',
              'minLength', 'maxLength', 'minimum', 'maximum', 'pattern', 'required') if k in schema}
    if 'enum' in result:
        result['enum'] = result['enum'][:12]
    if depth < 3:
        if 'items' in schema:
            result['items'] = _shape(schema['items'], root, depth + 1)
        if 'properties' in schema:
            result['properties'] = {k: _shape(v, root, depth + 1) for k, v in list(schema['properties'].items())[:8]}
    return result


def _example(schema, root, depth=0):
    schema = _resolve(schema, root)
    if schema.get('examples'):
        return schema['examples'][0]
    if 'const' in schema:
        return schema['const']
    if 'enum' in schema:
        return schema['enum'][0]
    kind = schema.get('type')
    if kind == 'array':
        return [_example(schema.get('items', {}), root, depth + 1)] if depth < 3 else []
    if kind == 'object' and depth < 3:
        return {k: _example(v, root, depth + 1) for k, v in list(schema.get('properties', {}).items())[:8]
                if k in schema.get('required', [])}
    if kind in {'integer', 'number'}:
        return schema.get('minimum', 0)
    if kind == 'boolean':
        return True
    return '<text>'


def validation_failure(exc: ValidationError, *, tool_name=None, schema=None) -> dict:
    """Describe invalid fields without echoing submitted file bodies or secrets."""
    schema = schema if schema is not None else _schemas.get(tool_name)
    try:
        root = schema.model_json_schema() if schema is not None else {}
    except (ValueError, TypeError):
        # Diagnostics must never replace the original error with a schema error.
        root = {}
    fields = []
    errors = exc.errors(include_url=False)
    for error in errors[:4]:
        location = error['loc']
        expected = _field_schema(root, location) if location else {}
        item = {'field': '.'.join(map(str, location)) or 'arguments', 'message': error['msg'][:400]}
        if expected:
            # One of the two: an example when it is short, else the expected shape.
            example = _example(expected, root)
            if len(json.dumps(example, ensure_ascii=False)) <= 600:
                item['example'] = example
            else:
                item['expected_schema'] = _shape(expected, root)
        if error['type'] == 'extra_forbidden':
            item['allowed_fields'] = list(_resolve(root, root).get('properties', {}))[:40]
        fields.append(item)
    return failure_result(validation_error_message(exc), 'invalid_arguments', 'invalid_arguments',
        fields=fields, omitted_errors=max(0, len(errors)-len(fields)),
        **({'example_call': EXAMPLE_CALLS[tool_name]} if tool_name in EXAMPLE_CALLS else {}))


def exception_result(exc: Exception, *, tool_name=None, schema=None) -> dict:
    # Cancellation is BaseException and must propagate so stopping/reset works.
    source = exc.source if isinstance(exc, ToolInvocationError) else exc
    if isinstance(exc, ToolInvocationError):
        tool_name = exc.tool_name
    if isinstance(source, ValidationError):
        return validation_failure(source, tool_name=tool_name, schema=schema)
    if type(source).__name__ == 'BasisChanged':
        # Names the changed sections; the generic conflict advice would send the member
        # back to re-read whole documents.
        return source.result()
    if isinstance(source, ToolFailure):
        return source.result
    reason = str(source)[:2500]
    if isinstance(source, PermissionError):
        category, code, policy = 'permission_denied', 'permission_denied', 'after_permission'
        advice = 'Respect the denial. Do not bypass the sandbox or repeat a user-denied operation. Report the boundary; request authorized access only when appropriate.'
    elif isinstance(source, FileNotFoundError):
        category, code, policy = 'not_found', 'not_found', 'after_state_change'
        advice = 'Inspect available paths/executables with browse_directory or detect_environment, then correct the path or prepare the missing dependency.'
    elif isinstance(source, (ConnectionError, TimeoutError)):
        category, code, policy = 'service_unavailable', 'timeout' if isinstance(source, TimeoutError) else 'connection_failed', 'after_service_ready'
        advice = 'Inspect service/process status and logs; confirm the correct endpoint is ready before retrying. Do not change valid parameters merely because a service is unavailable.'
    elif type(source).__name__ == 'SandboxUnavailable' or isinstance(source, ImportError):
        category, code, policy = 'environment_unavailable', 'missing_capability', 'after_environment_change'
        advice = 'Inspect environment capabilities and report the missing runtime/sandbox. Prepare required dependencies only within authorized scope; do not bypass isolation.'
    elif type(source).__name__ == 'StorageConflictError':
        # Optimistic-lock and state conflicts are expected coordination outcomes, not defects.
        category, code, policy = 'operation_rejected', 'state_conflict', 'after_state_change'
        advice = ('The stored state moved on (newer version, changed dependency or task state). Re-read the current '
                  'document/task/goal, reconcile your change with it, then retry with the current version; if the '
                  'state does not allow this operation, report that instead of retrying.')
    elif isinstance(source, (ValueError, LookupError)):
        # The message states the constraint; generic advice would add nothing.
        category, code, policy, advice = 'operation_rejected', 'invalid_state_or_value', 'inspect_before_retry', ''
    else:
        category, code, policy = 'internal_error', 'tool_execution_failed', 'do_not_retry_unchanged'
        advice = 'This may be a tool/runtime defect. Preserve the error and report the blocker; use another supported verification method if appropriate. Do not repeat the same failing operation or claim success.'
        logger.error('Tool execution failed', exc_info=(type(source), source, source.__traceback__))
    return failure_result(reason, category, code, advice)


def tool_error(exc: Exception) -> str:
    # ToolNode error handlers return strings; JSON becomes the next ToolMessage.
    return json.dumps(exception_result(exc), ensure_ascii=False)


DENIED_MARKERS = ('operation not permitted', 'permission denied', 'eperm', 'eacces')


def denied_path(output: str) -> str | None:
    """The path a command was refused, from the first output line that names one beside
    the refusal: quoted, or just before the refusal as shells print it ("ls: path: ...")."""
    import re
    def pathlike(value):
        return ('/' in value or value.startswith('.')) and '://' not in value
    for line in output.splitlines():
        lowered = line.lower()
        if not any(marker in lowered for marker in DENIED_MARKERS):
            continue
        for quoted in re.findall(r"""['"`]([^'"`]+)['"`]""", line):
            if pathlike(quoted):
                return quoted
        if before := re.search(r'^(.*?): (?:operation not permitted|permission denied)', line, re.I):
            segment = before.group(1)
            if (at := re.search(r'(?:^|\s)(/.*)$', segment)):
                return at.group(1)  # an absolute path may contain spaces
            token = segment.split()[-1] if segment.split() else ''
            if pathlike(token):
                return token
    return None


def permission_advice(output: str) -> str:
    """Which boundary of the command sandbox a refusal hit, and what to do about it. A
    command reads the system runtimes and reads and writes only its workspace and its
    temporary directory; sockets need network access."""
    import os
    from pathlib import Path
    from app.agents.tools._workspace import source_workspace, workspace_root
    from app.execution.sandbox import GUARDED_NAMES
    path = denied_path(output)
    if path is None:
        return ('No file path was refused, so this is the network boundary: a command that listens on a port or '
                'connects to one needs network access. Run it again with network=true; it is reviewed, not bypassed.')
    workspace = workspace_root().resolve()
    source = source_workspace.get()
    source = source.resolve() if source else workspace
    target = Path(os.path.normpath(path))
    outside = ('Commands read the system runtimes and read and write only the workspace and their temporary directory '
               '($TMPDIR, also $HOME).')
    if not target.is_absolute():
        if '..' in target.parts:
            return f'{path} climbs out of your workspace. {outside} Use paths inside the directory your commands start in.'
        parts = target.parts
    elif target == workspace or target.is_relative_to(workspace):
        parts = target.relative_to(workspace).parts
        cursor = workspace
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink() and not cursor.resolve().is_relative_to(workspace):
                link = cursor.relative_to(workspace).as_posix()
                return (f'{link} is a link to {cursor.resolve()}, outside your workspace, and commands cannot write through '
                        f'it. Remove the link itself (execute_command: rm {link}) and create your own {cursor.name} inside '
                        'the workspace (for dependencies: install them there).')
    elif source != workspace and (target == source or target.is_relative_to(source)):
        return (f'{path} is in the project root. Your task works on its own copy of the project, in the directory your '
                'commands start in, and commands cannot read or write the root. Something names the root by absolute path: '
                'use paths relative to cwd, and delete and regenerate any cache or generated configuration that recorded '
                'the old absolute path.'
                + (' Your workspace has its own dependency folder: install what is missing there.'
                   if {'node_modules', '.venv'} & set(target.parts) else ''))
    else:
        return (f'{path} is outside your workspace. {outside} Point the tool\'s cache, configuration or output at a path '
                'inside the workspace or $TMPDIR.')
    if guarded := next((part for part in parts if part in GUARDED_NAMES), None):
        return (f'{guarded} may not be created or changed inside the project (repository metadata, agent configuration '
                'and credentials are protected). Do the work without it.')
    if any(part == '.env' or part.startswith('.env.') and part not in {'.env.example', '.env.sample'} for part in parts):
        return ('Secret .env files cannot be read or written by commands. Use .env.example for templates; connected '
                'services arrive as environment variables.')
    return ('The path is inside your workspace, where the sandbox allows reads and writes: this is the file\'s own mode '
            'or owner (a script that is not executable, a read-only file). Check it with ls -l and fix the mode.')


def command_failure(message: str, output: str = '', *, timed_out=False) -> dict:
    """Only classify explicit evidence; a generic nonzero exit is not proof of outage."""
    text = output.lower()
    if timed_out:
        category, code, policy = 'timeout', 'command_timeout', 'inspect_before_retry'
        advice = 'Inspect progress/logs and the timeout. Use an appropriate bounded background lifetime for long work; retry only after resolving the cause.'
    elif any(marker in text for marker in ('address already in use', 'eaddrinuse')):
        category, code, policy = 'service_unavailable', 'port_in_use', 'after_state_change'
        advice = 'Inspect owned processes and choose a free port; synchronize the endpoint with dependent tasks. Do not terminate unowned services.'
    elif any(marker in text for marker in DENIED_MARKERS):
        category, code, policy = 'permission_denied', 'os_permission_denied', 'after_permission'
        advice = permission_advice(output)
    elif any(marker in text for marker in ('could not connect to server', 'connection refused', 'econnrefused')):
        category, code, policy = 'service_unavailable', 'connection_failed', 'after_service_ready'
        advice = 'Check the correct host/port and manage_process status/logs; start or wait for the authorized service before retrying.'
    else:
        # exit_code and output are the diagnosis; there is no more specific advice.
        category, code, policy, advice = 'command_failed', 'nonzero_exit', 'inspect_before_retry', ''
    return failure_result(message, category, code, advice, policy)


def normalize_failure(output: dict, *, tool_name=None) -> dict:
    if not tool_result_failed(output):
        return output
    if output.get('error_details'):
        return output
    failure = failure_result(str(output.get('error') or 'Operation reported a failed check'),
                             'operation_failed', 'failed_result')
    return {**output, **failure}


def tool_result_failed(output) -> bool:
    if getattr(output, 'status', None) == 'error':
        return True
    # ToolNode may wrap a structured collaboration/service result in ToolMessage.
    value = getattr(output, 'content', output)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return False
    return isinstance(value, dict) and (bool(value.get('error')) or value.get('passed') is False)
