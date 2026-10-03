"""Strict schemas and recoverable agent-facing tool errors."""
import json
import logging
from langchain_core.tools import tool as langchain_tool
from pydantic import ConfigDict
from pydantic import ValidationError
from langgraph.prebuilt.tool_node import ToolInvocationError

logger = logging.getLogger(__name__)
_schemas = {}


def tool(*args, **kwargs):
    def strict(result):
        result.args_schema.model_config = ConfigDict(**{**result.args_schema.model_config, 'strict': True, 'extra': 'forbid'})
        result.args_schema.model_rebuild(force=True)
        _schemas[result.name] = result.args_schema
        return result
    result = langchain_tool(*args, **kwargs)
    if hasattr(result, 'args_schema'):
        return strict(result)
    return lambda fn: strict(result(fn))


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
        schemas.append(schema)
    return schemas


def validation_error_message(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_input=False)
    lines = [f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in errors[:8]]
    if len(errors) > 8:
        lines.append(f'{len(errors) - 8} additional validation errors omitted')
    return 'Invalid arguments: ' + '; '.join(lines)[:2200]


def failure_result(message: str, category: str, code: str, next_action: str,
                   retry_policy: str = 'inspect_before_retry', **details) -> dict:
    """Keep the legacy error string while providing machine-readable recovery advice."""
    return {'error': message[:2500], 'error_details': {
        'category': category, 'code': code, 'retry_policy': retry_policy,
        'next_action': next_action, **details,
    }}


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


def _json_type(value):
    if value is None:
        return 'null'
    return {str: 'string', list: 'array', dict: 'object', bool: 'boolean', int: 'integer', float: 'number'}.get(type(value), type(value).__name__)


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
        item = {'field': '.'.join(map(str, location)) or 'arguments',
                'message': error['msg'][:400], 'code': error['type']}
        if error['type'] == 'missing':
            item['received_type'] = 'missing'
        elif 'input' in error:
            item['received_type'] = _json_type(error['input'])
        if expected:
            item['expected_schema'] = _shape(expected, root)
            example = _example(expected, root)
            if len(json.dumps(example, ensure_ascii=False)) <= 600:
                item['example'] = example
        if error['type'] == 'extra_forbidden':
            item['allowed_fields'] = list(_resolve(root, root).get('properties', {}))[:40]
        fields.append(item)
    return failure_result(validation_error_message(exc), 'invalid_arguments', 'invalid_arguments',
        'Correct the named fields using expected_schema/example. Arrays and objects must be JSON values, '
        'not JSON-encoded strings. Re-read exact source text if needed; do not repeat unchanged arguments. '
        'The operation was not executed.', 'correct_arguments', tool=tool_name, fields=fields,
        omitted_errors=max(0, len(errors)-len(fields)))


def exception_result(exc: Exception, *, tool_name=None, schema=None) -> dict:
    # Cancellation is BaseException and must propagate so stopping/reset works.
    source = exc.source if isinstance(exc, ToolInvocationError) else exc
    if isinstance(exc, ToolInvocationError):
        tool_name = exc.tool_name
    if isinstance(source, ValidationError):
        return validation_failure(source, tool_name=tool_name, schema=schema)
    if isinstance(source, ToolFailure):
        return {**source.result, 'error_details': {**source.result['error_details'],
                'tool': tool_name, 'exception_type': type(source).__name__}}
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
        category, code, policy = 'operation_rejected', 'invalid_state_or_value', 'inspect_before_retry'
        advice = 'Inspect the stated constraint and current file/request state. Correct the value or use the documented supported operation before retrying.'
    else:
        category, code, policy = 'internal_error', 'tool_execution_failed', 'do_not_retry_unchanged'
        advice = 'This may be a tool/runtime defect. Preserve the error and report the blocker; use another supported verification method if appropriate. Do not repeat the same failing operation or claim success.'
        logger.error('Tool execution failed', exc_info=(type(source), source, source.__traceback__))
    return failure_result(reason, category, code, advice, policy, tool=tool_name,
                          exception_type=type(source).__name__)


def tool_error(exc: Exception) -> str:
    # ToolNode error handlers return strings; JSON becomes the next ToolMessage.
    return json.dumps(exception_result(exc), ensure_ascii=False)


def command_failure(message: str, output: str = '', *, timed_out=False) -> dict:
    """Only classify explicit evidence; a generic nonzero exit is not proof of outage."""
    text = output.lower()
    if timed_out:
        category, code, policy = 'timeout', 'command_timeout', 'inspect_before_retry'
        advice = 'Inspect progress/logs and the timeout. Use an appropriate bounded background lifetime for long work; retry only after resolving the cause.'
    elif any(marker in text for marker in ('address already in use', 'eaddrinuse')):
        category, code, policy = 'service_unavailable', 'port_in_use', 'after_state_change'
        advice = 'Inspect owned processes and choose a free port; synchronize the endpoint with dependent tasks. Do not terminate unowned services.'
    elif any(marker in text for marker in ('operation not permitted', 'permission denied', 'eperm', 'eacces')):
        category, code, policy = 'permission_denied', 'os_permission_denied', 'after_permission'
        advice = 'Inspect the sandbox/network boundary and command logs. A local listener also needs reviewed network permission. Do not bypass isolation or a user denial.'
    elif any(marker in text for marker in ('could not connect to server', 'connection refused', 'econnrefused')):
        category, code, policy = 'service_unavailable', 'connection_failed', 'after_service_ready'
        advice = 'Check the correct host/port and manage_process status/logs; start or wait for the authorized service before retrying.'
    else:
        category, code, policy = 'command_failed', 'nonzero_exit', 'inspect_before_retry'
        advice = 'Inspect exit_code and output to determine whether this is a check failure, missing dependency or command-specific status. Correct the cause before retrying.'
    return failure_result(message, category, code, advice, policy)


def normalize_failure(output: dict, *, tool_name=None) -> dict:
    if not tool_result_failed(output):
        return output
    if output.get('error_details'):
        details = {**output['error_details']}
        if tool_name and not details.get('tool'):
            details['tool'] = tool_name
        return {**output, 'error_details': details}
    failure = failure_result(str(output.get('error') or 'Operation reported a failed check'),
        'operation_failed', 'failed_result',
        'Inspect the returned diagnostics and current state; correct the cause or choose another supported method. '
        'Do not repeat unchanged calls or report this operation as completed.', tool=tool_name)
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
