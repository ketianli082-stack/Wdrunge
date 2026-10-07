"""Fixed per-call overhead stays within budget, and every worker can talk to colleagues."""
from app.agents.overhead import role_overhead

# Estimated tokens (about twice real tokens for English). Before role tool profiles and
# prompt modules: Leader ~24.3K, workers ~18.4K-21.9K per call.
# 2026-10-06: tool parameters gained one-line descriptions with examples (a weak model
# fills arguments from the schema); backend and frontend went a little over their old
# 13,500 / 14,500 and the budget was raised for them rather than cutting instructions.
BUDGET = {'leader': 13500, 'product': 10500, 'designer': 10500, 'backend': 14000, 'frontend': 15000, 'testing': 14500}


def test_role_overhead_stays_within_budget(monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'budget')
    overhead = role_overhead()
    for role, limit in BUDGET.items():
        assert overhead[role]['total_tokens'] <= limit, (role, overhead[role])
    for role in ('product', 'designer', 'backend', 'frontend', 'testing'):
        assert {'collaboration_send', 'collaboration_request'} <= set(overhead[role]['tools']), role
    assert 'browser_verify' in overhead['testing']['tools'] and 'execute_command' in overhead['backend']['tools']
    assert 'execute_command' not in overhead['product']['tools']
