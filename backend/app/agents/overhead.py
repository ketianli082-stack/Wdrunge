"""Fixed per-call overhead of each role: system prompt and bound tool schemas.

Every model call resends both, so they bound how cheap a call can be. Used by the
benchmark script and by tests that hold the budget. Token counts use the same
conservative estimate as context memory (about twice real tokens for English).
"""
import json


def role_overhead() -> dict:
    from app.agents.memory import estimate_tokens
    from app.agents.team import Team
    from app.agents.tools.validation import model_tool_schemas
    from app.collaboration.tools import Context, active
    team = Team()
    result = {}
    for role, agent in {'leader': team.leader, **team.workers}.items():
        token = active.set(Context(None, 'project', role, 'conversation', 'benchmark', request=None if role == 'leader' else 'request'))
        try:
            tools = agent.bound_tools([]) if hasattr(agent, 'bound_tools') else []
            prompt = agent.full_system_prompt() if hasattr(agent, 'full_system_prompt') else agent.system_prompt()
        finally:
            active.reset(token)
        schemas = model_tool_schemas(tools)
        result[role] = {'tools': [t.name for t in tools], 'tool_count': len(tools),
                        'schema_tokens': estimate_tokens(json.dumps(schemas, ensure_ascii=False)) if schemas else 0,
                        'prompt_tokens': estimate_tokens(prompt)}
        result[role]['total_tokens'] = result[role]['schema_tokens'] + result[role]['prompt_tokens']
    return result
