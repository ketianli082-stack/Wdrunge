"""Which tools each role sees on a model call.

Every bound tool's schema is resent on every call, and a changed tool list
invalidates the provider's prompt cache from the start. So each role sees a core
set chosen from how the role works (communication included for every worker), plus
groups implied by its task (declared deliverables), fixed for the whole task.
Other groups load with enable_tools and stay loaded. Every tool stays executable;
visibility only decides what the model is offered.
"""
from __future__ import annotations

GROUPS: dict[str, set[str]] = {
    'files': {'read_project_file', 'write_project_file', 'search_code', 'browse_directory'},
    'files_more': {'list_project_files', 'manage_file', 'read_external_file'},
    'commands': {'execute_command', 'manage_process', 'detect_environment'},
    'browser': {'browser_verify'},
    'project_checks': {'run_project_check', 'run_service_check', 'refresh_api_contract', 'refresh_frontend_client'},
    'documents': {'collaboration_read', 'collaboration_publish'},
    'documents_more': {'collaboration_catalog', 'collaboration_search', 'collaboration_diff', 'collaboration_history',
                       'collaboration_subscribe'},
    'team': {'collaboration_send', 'collaboration_request', 'collaboration_inspect_request', 'collaboration_wait'},
    'team_more': {'collaboration_inspect_message', 'collaboration_snapshot', 'collaboration_dependencies',
                  'collaboration_schedule', 'collaboration_handoff'},
    'delivery': {'submit_task', 'collaboration_transition', 'collaboration_journal', 'collaboration_check_delivery'},
    'environment': {'collaboration_register_service'},
    'environment_more': {'collaboration_environment', 'collaboration_register_command'},
    'goals': {'create_goal', 'update_goal', 'get_goal', 'create_task', 'complete_task', 'complete_goal'},
    'planning_more': {'collaboration_decision', 'collaboration_maintenance', 'collaboration_recover'},
    'delegation': {'delegate_task', 'delegate_tasks'},
    'memory': {'checkpoint_context', 'recall_context'},
    'time': {'current_utc_time'},
}
GROUP_PURPOSE = {
    'files_more': 'tree listing, move/delete, outside config',
    'commands': 'commands and services',
    'browser': 'browser checks',
    'project_checks': 'fixed-layout checks',
    'documents_more': 'doc catalog, search, diff, history',
    'team_more': 'messages, snapshot, scheduling, handoff',
    'environment_more': 'full registry, named commands',
    'goals': 'goals and tasks',
    'planning_more': 'decisions, doc maintenance, recovery',
    'delegation': 'direct delegation',
    'time': 'current time',
}
ROLE_GROUPS: dict[str, set[str]] = {
    'leader': {'goals', 'documents', 'environment', 'memory', 'delegation'},
    'product': {'files', 'documents', 'team', 'delivery', 'memory', 'time'},
    'designer': {'files', 'documents', 'team', 'delivery', 'memory'},
    'backend': {'files', 'commands', 'documents', 'team', 'delivery', 'environment', 'memory'},
    'frontend': {'files', 'commands', 'browser', 'documents', 'team', 'delivery', 'environment', 'memory'},
    'testing': {'files', 'commands', 'browser', 'documents', 'team', 'delivery', 'environment', 'memory'},
}
# Leader core beyond its groups: inspecting and moving tasks, messaging, decisions.
ROLE_EXTRA = {'leader': {'collaboration_inspect_request', 'collaboration_transition', 'collaboration_send', 'collaboration_decision'}}
ROLE_HIDDEN = {'leader': {'collaboration_publish', 'delegate_task'}}

GROUP_OF = {name: group for group, names in GROUPS.items() for name in names}


def task_groups(task: dict | None, role: str) -> set[str]:
    """Groups implied by the task itself, chosen once when it starts."""
    if not task:
        return set()
    groups = set()
    if task.get('services') and role in {'backend', 'frontend', 'testing'}:
        groups |= {'commands', 'environment'}
    if task.get('verification') == 'end_to_end' and role in {'frontend', 'testing'}:
        groups.add('browser')
    if task.get('documents'):
        groups.add('documents')
    return groups


def enabled_groups(messages) -> set[str]:
    """Groups enabled earlier in this context (and tools already used)."""
    groups, used = set(), set()
    for message in messages:
        for call in getattr(message, 'tool_calls', None) or ():
            if call.get('name') == 'enable_tools':
                args = call.get('args') or {}
                groups.update(g for g in args.get('groups') or () if isinstance(g, str))
                # Older calls enabled individual tools by name.
                groups.update(GROUP_OF[n] for n in args.get('names') or () if isinstance(n, str) and n in GROUP_OF)
            else:
                used.add(call.get('name'))
    return groups | {GROUP_OF[name] for name in used if name in GROUP_OF}


def select_tools(role: str, available, messages=(), task: dict | None = None):
    """The tools offered to the model, in a stable order."""
    shown_groups = ROLE_GROUPS.get(role, set(GROUPS)) | task_groups(task, role) | enabled_groups(messages)
    hidden = set(ROLE_HIDDEN.get(role, ()))
    used = {call.get('name') for m in messages for call in getattr(m, 'tool_calls', None) or ()}
    if role != 'leader':
        # One delivery verb per task: submit_task for goal tasks, transition otherwise;
        # a worker that requested a colleague's fix also accepts it with transition.
        if task and task.get('goal_id'):
            if not used & {'collaboration_request', 'collaboration_wait'}:
                hidden.add('collaboration_transition')
        else:
            hidden.add('submit_task')
    shown, offered_groups = [], set()
    for tool in available:
        group = GROUP_OF.get(tool.name)
        if tool.name == 'enable_tools':
            continue  # offered below only while some group is still hidden
        visible = (tool.name in used or tool.name in ROLE_EXTRA.get(role, ())
                   or (group is None and tool.name != 'enable_tools')
                   or (group in shown_groups and tool.name not in hidden))
        if visible:
            shown.append(tool)
        elif group:
            offered_groups.add(group)
    enabler = next((t for t in available if t.name == 'enable_tools'), None)
    if enabler and offered_groups - shown_groups:
        shown.append(enabler)
    return shown


def loadable_groups(role: str, available) -> list[str]:
    names = {tool.name for tool in available}
    return sorted(group for group, members in GROUPS.items()
                  if group not in ROLE_GROUPS.get(role, set()) and members & names)
