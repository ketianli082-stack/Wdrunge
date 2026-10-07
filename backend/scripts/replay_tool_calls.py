"""Replay recorded tool calls through today's argument handling and model views.

    python scripts/replay_tool_calls.py <calls.json>

calls.json: rows of the tool_calls table for one conversation, e.g.
    sqlite3 -json "file:<app db>?mode=ro" "select tool_call_id,agent_id,tool,status,arguments,result,created_at
      from tool_calls where conversation_id='<id>' order by created_at" > calls.json

Reports, without calling any tool:
- arguments: calls that succeeded must come out of normalization unchanged and still
  validate; calls that failed on their arguments are listed with what happens now;
- results: characters of each tool's results as recorded and as the model would be
  shown them now (the log truncates arguments and results at 8,000 characters; rows
  that no longer parse are counted as unchanged).
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GENERIC_ADVICE = ('Inspect exit_code and output', 'Inspect the stated constraint', 'Inspect the returned diagnostics',
                  'Correct the named fields using expected_schema')


def tools_by_role():
    import os
    os.environ.setdefault('DASHSCOPE_API_KEY', 'replay')
    from app.agents.team import Team
    from app.collaboration.tools import COLLABORATION_TOOLS, WORKER_REQUEST, enable_tools
    team = Team()
    shared = {**COLLABORATION_TOOLS, enable_tools.name: enable_tools}
    roles = {'leader': {**shared, **{t.name: t for t in team.leader.spec.tools}}}
    for role, worker in team.workers.items():
        roles[role] = {**shared, WORKER_REQUEST.name: WORKER_REQUEST, **{t.name: t for t in worker.spec.tools}}
    return roles


def slim_failure(text):
    try:
        data = json.loads(text)
    except ValueError:
        return text
    details = data.get('error_details') if isinstance(data, dict) else None
    if not isinstance(details, dict):
        return text
    details = {k: v for k, v in details.items() if k not in ('retry_policy', 'tool', 'exception_type')}
    if any(str(details.get('next_action', '')).startswith(prefix) for prefix in GENERIC_ADVICE):
        details.pop('next_action')
    for item in details.get('fields') or []:
        if isinstance(item, dict):
            item.pop('code', None), item.pop('received_type', None)
            if 'example' in item:
                item.pop('expected_schema', None)
    return json.dumps({**data, 'error_details': details}, ensure_ascii=False)


def main(path):
    from app.agents.tools.arguments import normalize_arguments
    from app.agents.tools.validation import model_result
    roles = tools_by_role()
    rows = json.loads(Path(path).read_text())
    changed, broken, skipped, fixed, still = [], [], 0, [], []
    sizes = defaultdict(lambda: [0, 0, 0])
    failures = [0, 0, 0]
    for row in rows:
        tool = roles.get(row['agent_id'], {}).get(row['tool'])
        try:
            arguments = json.loads(row['arguments'])
        except ValueError:
            arguments = None
        if row['status'] == 'success':
            shown = model_result(row['tool'], arguments or {}, row['result'])
            size = sizes[row['tool']]
            size[0], size[1], size[2] = size[0] + 1, size[1] + len(row['result']), size[2] + len(shown)
        else:
            failures[0], failures[1], failures[2] = failures[0] + 1, failures[1] + len(row['result']), failures[2] + len(slim_failure(row['result']))
        if tool is None or arguments is None:
            skipped += 1
            continue
        normalized = normalize_arguments(tool, arguments)
        error = normalized.failure['error'] if normalized.failure else None
        if error is None:
            try:
                tool.args_schema.model_validate(normalized.args)
            except ValueError as exc:
                error = str(exc).splitlines()[-2].strip() if len(str(exc).splitlines()) > 1 else str(exc)
        if row['status'] == 'success':
            if normalized.args != arguments or normalized.ignored:
                changed.append((row['tool'], normalized.ignored or 'rewritten'))
            if error:
                broken.append((row['tool'], error[:160]))
        elif 'invalid_arguments' in row['result'] or 'Invalid arguments' in row['result']:
            (still if error else fixed).append((row['created_at'][11:19], row['agent_id'], row['tool'],
                                                error[:140] if error else f'accepted; ignored={normalized.ignored}, repaired={normalized.repaired}'))
    print(f'{len(rows)} calls; {skipped} skipped (unknown tool or arguments cut at the log limit)')
    print(f'\nArguments of successful calls changed by normalization: {len(changed)}')
    for item in changed[:20]:
        print('  ', item)
    print(f'Successful calls that would now fail validation: {len(broken)}')
    for item in broken[:20]:
        print('  ', item)
    print(f'\nCalls that failed on their arguments and are accepted now: {len(fixed)}')
    for item in fixed:
        print('  ', *item)
    print(f'Calls that failed on their arguments and still fail: {len(still)}')
    for item in still:
        print('  ', *item)
    print('\nResults shown to the model (characters)')
    print(f"  {'tool':34} {'calls':>5} {'recorded':>10} {'now':>10} {'saved':>8}")
    total = [0, 0]
    for name, (count, before, after) in sorted(sizes.items(), key=lambda item: item[1][2] - item[1][1]):
        total[0], total[1] = total[0] + before, total[1] + after
        if before != after:
            print(f'  {name:34} {count:5} {before:10,} {after:10,} {before - after:8,}')
    print(f"  {'(tools without a view)':34} {'':5} {'':10} {'':10} {0:8,}")
    print(f"  {'all successful results':34} {sum(s[0] for s in sizes.values()):5} {total[0]:10,} {total[1]:10,} {total[0] - total[1]:8,}")
    print(f"  {'failed results':34} {failures[0]:5} {failures[1]:10,} {failures[2]:10,} {failures[1] - failures[2]:8,}")
    everything = total[0] + failures[1]
    saved = total[0] - total[1] + failures[1] - failures[2]
    print(f'  saved {saved:,} of {everything:,} characters ({saved * 100 / everything:.1f}%)')


if __name__ == '__main__':
    main(sys.argv[1])
