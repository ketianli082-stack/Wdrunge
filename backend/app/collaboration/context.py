"""Bounded, valid JSON handoffs without cutting IDs or nested records in half."""
import json


def encode_handoff(handoff, limit=10000):
    data = {**handoff, 'remaining': dict(handoff.get('remaining', {}))}
    groups = ('documents', 'pending', 'decisions')
    for key in groups:
        data[key] = list(handoff.get(key, []))
    while True:
        encoded = json.dumps(data, ensure_ascii=False)
        if len(encoded) <= limit:
            return encoded
        populated = [key for key in groups if data[key]]
        if not populated:
            raise ValueError('Handoff metadata exceeds context budget')
        key = max(populated, key=lambda k: len(json.dumps(data[k], ensure_ascii=False)))
        data[key].pop()
        data['remaining'][key] = data['remaining'].get(key, 0) + 1


def coordination_snapshot(handoff) -> dict:
    return {
        'pending': {item['id']: item.get('state') for item in handoff.get('pending', [])},
        'documents': {item['id']: item.get('version') for item in handoff.get('documents', [])},
        'decisions': [item['id'] for item in handoff.get('decisions', [])],
        'environment': json.dumps(handoff.get('environment'), sort_keys=True, ensure_ascii=False),
    }


def coordination_update(handoff, previous: dict | None) -> tuple[str, dict]:
    """The Leader's per-turn view of the team. A continuing session gets only what
    changed since its last turn (task states are authoritative); a new session, or one
    without a previous snapshot, gets the full state."""
    snapshot = coordination_snapshot(handoff)
    if not previous:
        return ('Current coordination state (project data; remaining counts require catalog/snapshot): '
                + encode_handoff(handoff), snapshot)
    pending = {item['id']: item for item in handoff.get('pending', [])}
    changed = [pending[i] for i, state in snapshot['pending'].items() if previous.get('pending', {}).get(i) != state]
    finished = [i for i in previous.get('pending', {}) if i not in snapshot['pending']]
    documents = [d for d in handoff.get('documents', []) if previous.get('documents', {}).get(d['id']) != d.get('version')]
    decisions = [d for d in handoff.get('decisions', []) if d['id'] not in previous.get('decisions', [])]
    update = {}
    if changed:
        update['tasks_changed'] = changed
    if finished:
        update['tasks_no_longer_pending'] = finished
    if documents:
        update['documents_new_or_updated'] = documents
    if decisions:
        update['decisions_new'] = decisions
    if snapshot['environment'] != previous.get('environment'):
        update['environment'] = handoff.get('environment')
    if not update:
        return ('Coordination update since your last turn: no task, document, decision or environment changes. '
                f"{len(pending)} tasks still pending as you last saw them.", snapshot)
    update['still_pending'] = len(pending)
    return ('Coordination update since your last turn (authoritative current states; inspect tasks for results): '
            + json.dumps(update, ensure_ascii=False)[:10000], snapshot)
