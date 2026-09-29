import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveReview, reviewActions, type CharacterReview } from '../src/CharacterReview';
import type { DeskRole } from '../src/DeskScene';

test('manual review only offers each role its supported actions', () => {
  for (const role of ['leader', 'product', 'frontend', 'backend', 'designer', 'testing'] as DeskRole[]) {
    const actions = reviewActions(role);
    assert.equal(actions.includes('gaming'), role === 'backend' || role === 'testing');
    assert.equal(actions.includes('drawing'), role === 'designer');
    assert.equal(actions.includes('typing'), role !== 'designer');
    assert.equal(actions.includes('mouse'), role !== 'designer');
    for (const action of ['rest', 'sleeping', 'phone', 'drinking', 'stretching'] as const) assert.ok(actions.includes(action));
  }
});

test('manual actions choose a valid visual status; automatic returns to the actual status', () => {
  const selection = (action: CharacterReview['action']): CharacterReview => ({ action, time: 0, playing: true, revision: 1 });
  assert.deepEqual(resolveReview('frontend', 'idle', selection('typing')), { state: 'running', action: 'typing' });
  assert.deepEqual(resolveReview('backend', 'running', selection('gaming')), { state: 'idle', action: 'gaming' });
  assert.deepEqual(resolveReview('designer', 'running', selection('stretching')), { state: 'idle', action: 'stretching' });
  assert.deepEqual(resolveReview('backend', 'failed'), { state: 'failed', action: null });
  assert.deepEqual(resolveReview('frontend', 'idle', selection('gaming')), { state: 'idle', action: null });
  assert.deepEqual(resolveReview('designer', 'running', selection('typing')), { state: 'running', action: null });
});
