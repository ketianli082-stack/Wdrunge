import test from 'node:test';
import assert from 'node:assert/strict';
import { sampleAction, canPlayAction, createCharacterBehavior, actionWeights } from '../src/CharacterBehavior.ts';

const roles = ['leader', 'product', 'designer', 'frontend', 'backend', 'testing'];
const seeded = () => {
  let seed = 713;
  return () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 2 ** 32);
};

test('real work permits stretching but forbids sleep, phone and games; failure always rests', () => {
  for (const role of roles) {
    assert.equal(canPlayAction(role, 'running', 'stretching'), true);
    for (const override of ['sleeping', 'phone', 'gaming']) assert.notEqual(sampleAction(role, 'running', 3, override).action, override);
    for (const override of [null, 'typing', 'drinking', 'sleeping', 'stretching']) assert.equal(sampleAction(role, 'failed', 4, override).action, 'rest');
  }
});

test('weighted choices match work probabilities and rest dominates idle for every role', () => {
  for (const role of roles) for (const state of ['running', 'idle']) {
    const behavior = createCharacterBehavior(role, seeded());
    let clip = behavior.update(0, state, 12);
    const counts = {}, seconds = {};
    for (let i = 0; i < 20000; i++) {
      clip = behavior.update(clip.duration + .01, state, 12);
      counts[clip.action] = (counts[clip.action] ?? 0) + 1;
      seconds[clip.action] = (seconds[clip.action] ?? 0) + clip.duration;
      assert.ok(canPlayAction(role, state, clip.action));
      if (clip.action === 'gaming') assert.equal(clip.input, 'keyboard-mouse');
      else assert.notEqual(clip.input, 'keyboard-mouse');
    }
    if (state === 'idle') {
      // The two-rest limit intentionally conditions subsequent draws. Rest
      // should still occupy more time than any other individual activity.
      for (const [action, total] of Object.entries(seconds)) if (action !== 'rest') assert.ok(seconds.rest > total, `${role}: ${action} dominates rest`);
    } else {
      for (const [action, weight] of actionWeights(role, state)) assert.ok(Math.abs(counts[action] / 200 - weight) < 1.2, `${role}/${state}/${action}`);
      assert.ok(counts.drinking < counts.stretching);
    }
  }
});

test('working starts at a tool and repeated status updates do not resample or restart a bout', () => {
  for (const role of roles) {
    let calls = 0;
    const behavior = createCharacterBehavior(role, () => { calls++; return .2; });
    const initial = behavior.update(0, 'running', 12), before = calls;
    assert.equal(initial.action, role === 'designer' ? 'drawing' : 'typing');
    for (let frame = 1; frame <= 300; frame++) {
      const clip = behavior.update(1 / 30, 'running', 12);
      assert.equal(clip.action, initial.action);
      assert.ok(Math.abs(clip.time - frame / 30) < .000001);
      assert.equal(calls, before);
    }
  }
});

function sleepingBehavior(hour = 23) {
  const behavior = createCharacterBehavior('frontend', () => hour === 12 ? .81 : .85);
  behavior.update(0, 'idle', hour);
  assert.equal(behavior.update(32, 'idle', hour).action, 'sleeping');
  behavior.update(3, 'idle', hour);
  return behavior;
}

test('night sleep stays settled across midnight; eight oclock plays rise, one stretch, then rest', () => {
  const behavior = sleepingBehavior();
  for (const hour of [23.5, 0, 3, 7, 7.999]) {
    const clip = behavior.update(200, 'idle', hour);
    assert.equal(clip.action, 'sleeping');
    assert.equal(clip.time, clip.duration / 2);
  }
  let clip = behavior.update(0, 'idle', 8);
  assert.equal(clip.action, 'sleeping');
  assert.equal(clip.time, clip.duration - 1.2);
  clip = behavior.update(1.21, 'idle', 8);
  assert.equal(clip.action, 'stretching');
  clip = behavior.update(7.01, 'idle', 8.1);
  assert.equal(clip.action, 'rest');
  assert.equal(behavior.update(1, 'idle', 8.2).action, 'rest');
});

test('a work task wakes a sleeper immediately and cancels the pending morning routine', () => {
  const behavior = sleepingBehavior();
  assert.ok(['typing', 'mouse'].includes(behavior.update(.03, 'running', 2).action));
  assert.notEqual(behavior.update(.03, 'running', 8).action, 'sleeping');
  assert.equal(behavior.update(.03, 'idle', 8).action, 'rest');
});

test('daytime naps finish; a clock jump past morning releases night sleep; reset clears sleep override', () => {
  const daytime = sleepingBehavior(12);
  assert.notEqual(daytime.update(12.1, 'idle', 12).time, 7.5);
  const skippedMorning = sleepingBehavior(23);
  assert.equal(skippedMorning.update(0, 'idle', 22).time, 13.8);
  const reset = sleepingBehavior();
  reset.reset();
  assert.equal(reset.update(0, 'idle', 23).action, 'rest');
});

test('manual single clips remain scrubbable, role restricted, and independent of night holding', () => {
  for (const role of roles) {
    const allowed = role === 'backend' || role === 'testing';
    assert.equal(sampleAction(role, 'idle', 3, 'gaming').action === 'gaming', allowed);
    assert.equal(canPlayAction(role, 'running', 'drawing'), role === 'designer');
    assert.equal(canPlayAction(role, 'running', 'typing'), role !== 'designer');
    assert.equal(sampleAction(role, 'idle', 15.5, 'sleeping').time, .5);
  }
});


test('awake idle never chains more than fourteen seconds of rest, even with repeated rest draws', () => {
  for (const role of roles) for (const value of [0, .59]) {
    const behavior = createCharacterBehavior(role, () => value);
    let clip = behavior.update(0, 'idle', 12), quietTime = 0, sawActivity = false;
    for (let i = 0; i < 30; i++) {
      if (clip.action === 'rest') {
        assert.ok(clip.duration >= 4 && clip.duration <= 7);
        quietTime += clip.duration;
        assert.ok(quietTime <= 14);
      } else { quietTime = 0; sawActivity = true; }
      clip = behavior.update(clip.duration + .01, 'idle', 12);
    }
    assert.ok(sawActivity);
  }
  const night = createCharacterBehavior('frontend', () => 0);
  assert.equal(night.update(0, 'idle', 23).duration, 18);
});
