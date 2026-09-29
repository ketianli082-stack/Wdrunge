import './character-behavior.test.mjs';
import './office-daylight.test';
import './screen-animation.test';
import './work-animation.test';
import './character-review.test';
import test from 'node:test';
import assert from 'node:assert/strict';
import * as THREE from 'three';
import { createDeskModel, type DeskRole } from '../src/DeskScene';
import { createOfficeCharacter } from '../src/OfficeCharacter';
import { advanceWork } from '../src/WorkAnimation';

// Only screen painting is stubbed; geometry, poses and IK use the real rig.
const context = new Proxy({}, { get: () => () => {} });
Object.assign(globalThis, { document: { createElement: () => ({ getContext: () => context }) } });
const roles: DeskRole[] = ['leader', 'product', 'designer', 'frontend', 'backend', 'testing'];

test('unchanged running status continues through multiple work cycles without resetting screen progress', () => {
  for (const role of roles) {
    const rig = createOfficeCharacter(role, createDeskModel(role), () => .2);
    const progress = { elapsed: 0, editing: 0 };
    const actions = new Set<string>();
    let previousTime = 0;
    for (let frame = 0; frame < 120 * 30; frame++) {
      const clip = rig.update(1 / 30, 'running', true);
      assert.ok(clip.time === 0 || Math.abs(clip.time - previousTime - 1 / 30) < .000001);
      previousTime = clip.time;
      advanceWork(progress, 1 / 30, true, clip.input);
      actions.add(clip.action);
    }
    assert.ok(actions.has(role === 'designer' ? 'drawing' : 'typing'));
    assert.ok(!actions.has('gaming') && !actions.has('stretching'));
    assert.ok(progress.elapsed > 119 && progress.editing > 30);
  }
});

test('gaming keeps the left hand on keys and the right hand on the moving mouse', () => {
  for (const role of ['backend', 'testing'] as const) {
    const desk = createDeskModel(role), rig = createOfficeCharacter(role, desk);
    const left = rig.group.getObjectByName('left-hand')!, right = rig.group.getObjectByName('right-hand')!;
    let firstMouse: THREE.Vector3 | undefined;
    let moved = false;
    // Active play excludes the deliberate reach/release phases.
    for (let time = 1; time < 10.6; time += .05) {
      const clip = rig.update(0, 'idle', false, 'gaming', time);
      assert.equal(clip.input, 'keyboard-mouse');
      assert.ok(left.position.distanceTo(desk.interaction.keyboard.clone().add(new THREE.Vector3(-.22, 0, .015))) < .025);
      assert.ok(Math.abs(right.position.x - desk.interaction.mouse!.position.x) < .001);
      assert.ok(Math.abs(right.position.z - desk.interaction.mouse!.position.z) < .001);
      assert.ok(rig.group.userData.reachError < .005);
      firstMouse ??= desk.interaction.mouse!.position.clone();
      moved ||= firstMouse.distanceTo(desk.interaction.mouse!.position) > .03;
    }
    assert.ok(moved);
  }
});

test('gaming releases both hands to rest before the loop seam without dragging the mouse off the desk', () => {
  for (const role of ['backend', 'testing'] as const) {
    const desk = createDeskModel(role), rig = createOfficeCharacter(role, desk);
    const joints = ['left-hand', 'right-hand', 'left-elbow', 'right-elbow'].map(name => rig.group.getObjectByName(name)!);
    rig.update(0, 'idle', false, 'rest', 0);
    const rest = joints.map(joint => joint.position.clone());
    let previous: THREE.Vector3[] | undefined;
    for (let frame = 0; frame <= 1210; frame++) {
      const time = frame / 100;
      rig.update(0, 'idle', false, 'gaming', time);
      if (previous) joints.forEach((joint, i) => assert.ok(joint.position.distanceTo(previous![i]) < .035, `${role} ${joint.name} jumps at ${time}`));
      assert.equal(desk.interaction.mouse!.position.y, desk.interaction.mouseHome.y);
      assert.ok(desk.interaction.mouse!.position.distanceTo(desk.interaction.mouseHome) < .07);
      if (frame === 1199) joints.forEach((joint, i) => assert.ok(joint.position.distanceTo(rest[i]) < .012, `${role} did not return to rest`));
      previous = joints.map(joint => joint.position.clone());
    }
    let draw = 0;
    const live = createOfficeCharacter(role, createDeskModel(role), () => [ .99, .99, 0, 0 ][draw++ % 4]);
    const hand = live.group.getObjectByName('right-hand')!;
    let last = hand.position.clone(), played = false, rested = false;
    for (let frame = 0; frame < 60 * 30; frame++) {
      const clip = live.update(1 / 30, 'idle', true);
      if (clip.action === 'gaming') played = true;
      if (played && clip.action === 'rest') {
        rested = true;
        assert.ok(hand.position.distanceTo(last) < .035, `${role} snapped on game exit`);
      }
      last = hand.position.clone();
    }
    assert.ok(played && rested);
  }
});

test('stretch hands and elbows remain continuous across a full clip and loop seam', () => {
  for (const role of roles) {
    const rig = createOfficeCharacter(role, createDeskModel(role));
    const joints = ['left-hand', 'right-hand', 'left-elbow', 'right-elbow'].map(name => rig.group.getObjectByName(name)!);
    let previous: { position: THREE.Vector3; rotation: THREE.Quaternion }[] | undefined;
    for (let frame = 0; frame <= 710; frame++) {
      rig.update(0, 'idle', false, 'stretching', frame / 100);
      for (const [index, joint] of joints.entries()) {
        if (previous) {
          assert.ok(joint.position.distanceTo(previous[index].position) < .06, `${role} ${joint.name} position jumps at ${frame / 100}`);
          assert.ok(joint.quaternion.angleTo(previous[index].rotation) < .2, `${role} ${joint.name} rotation jumps at ${frame / 100}`);
        }
      }
      assert.ok(rig.group.userData.reachError < .005, `${role} stretch exceeds reach`);
      previous = joints.map(joint => ({ position: joint.position.clone(), rotation: joint.quaternion.clone() }));
    }
  }
});

test('palm keeps a fixed grip relative to cup during lift, inward turn, sip and replacement', () => {
  const desk = createDeskModel('backend');
  const rig = createOfficeCharacter('backend', desk);
  const hand = rig.group.getObjectByName('right-hand')!;
  let reference: THREE.Quaternion | undefined;
  for (let frame = 0; frame <= 595; frame++) {
    const time = frame / 100;
    rig.update(.01, 'idle', true, 'drinking', time);
    if (time < 1.1) continue;
    const inverse = desk.interaction.cup.quaternion.clone().invert();
    const localPosition = hand.position.clone().sub(desk.interaction.cup.position).applyQuaternion(inverse);
    const localRotation = inverse.multiply(hand.quaternion);
    reference ??= localRotation.clone();
    assert.ok(localPosition.distanceTo(new THREE.Vector3(0, .105, .15)) < .005, `grip slips at ${time}`);
    assert.ok(localRotation.angleTo(reference) < .001, `grip twists at ${time}`);
  }
});

test('live stretch enters and exits rest smoothly at 30 fps and pauses exactly', () => {
  let draw = 0;
  const rig = createOfficeCharacter('leader', createDeskModel('leader'), () => [.89, .89, 0, 0][draw++ % 4]);
  const joints = ['left-hand', 'right-hand', 'left-elbow', 'right-elbow'].map(name => rig.group.getObjectByName(name)!);
  let previous: THREE.Vector3[] | undefined;
  let sawStretch = false, returnedToRest = false;
  for (let frame = 0; frame < 2100; frame++) {
    const clip = rig.update(1 / 30, 'idle', true);
    if (clip.action === 'stretching') sawStretch = true;
    if (sawStretch && clip.action === 'rest') returnedToRest = true;
    if (clip.action === 'stretching' || returnedToRest) {
      if (previous) joints.forEach((joint, i) => assert.ok(joint.position.distanceTo(previous![i]) < .11, `live joint jumps at ${frame / 30}`));
    }
    previous = joints.map(joint => joint.position.clone());
    assert.deepEqual(rig.group.position.toArray(), [0, 0, 0]);
  }
  assert.ok(sawStretch && returnedToRest);
  const before = joints.map(joint => [joint.position.toArray(), joint.quaternion.toArray()]);
  rig.update(5, 'idle', false);
  joints.forEach((joint, i) => assert.deepEqual([joint.position.toArray(), joint.quaternion.toArray()], before[i]));
});

test('probabilistic work transitions keep hands and elbows continuous, including drawing and stretching', () => {
  for (const role of roles) {
    let seed = 32;
    const rig = createOfficeCharacter(role, createDeskModel(role), () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 2 ** 32));
    const joints = ['left-hand', 'right-hand', 'left-elbow', 'right-elbow'].map(name => rig.group.getObjectByName(name)!);
    let previous: THREE.Vector3[] | undefined;
    const actions = new Set<string>();
    for (let frame = 0; frame < 240 * 30; frame++) {
      const clip = rig.update(1 / 30, 'running', true);
      actions.add(clip.action);
      if (previous) joints.forEach((joint, i) => assert.ok(joint.position.distanceTo(previous![i]) < .12, `${role} ${clip.action} ${joint.name} jumps at ${frame / 30}`));
      previous = joints.map(joint => joint.position.clone());
    }
    assert.ok(actions.has('stretching'));
  }
});

test('work interrupts settled leisure with a continuous transition instead of teleporting hands or props', () => {
  for (const action of ['sleeping', 'phone', 'drinking', 'stretching', 'gaming'] as const) {
    const desk = createDeskModel('backend'), rig = createOfficeCharacter('backend', desk, () => .2);
    const joints = ['left-hand', 'right-hand', 'left-elbow', 'right-elbow'].map(name => rig.group.getObjectByName(name)!);
    rig.update(0, 'idle', false, action, 3.5);
    let previous = joints.map(joint => joint.position.clone()), cup = desk.interaction.cup.position.clone();
    for (let frame = 0; frame < 90; frame++) {
      const clip = rig.update(1 / 30, 'running', true);
      assert.equal(clip.action, 'typing');
      joints.forEach((joint, i) => assert.ok(joint.position.distanceTo(previous[i]) < .12, `${action} ${joint.name} jumps at ${frame}`));
      assert.ok(desk.interaction.cup.position.distanceTo(cup) < .08);
      previous = joints.map(joint => joint.position.clone()); cup = desk.interaction.cup.position.clone();
    }
    assert.ok(desk.interaction.cup.position.distanceTo(desk.interaction.cupHome) < .001);
  }
});

test('overnight rig holds its sleeping posture then rises and stretches continuously at eight', () => {
  const rig = createOfficeCharacter('frontend', createDeskModel('frontend'), () => .85);
  const joints = ['left-hand', 'right-hand', 'left-elbow', 'right-elbow'].map(name => rig.group.getObjectByName(name)!);
  for (let frame = 0; frame < 50 * 30; frame++) rig.update(1 / 30, 'idle', true, null, undefined, 23);
  const sleeping = joints.map(joint => joint.position.clone());
  for (let frame = 0; frame < 10 * 30; frame++) assert.equal(rig.update(1 / 30, 'idle', true, null, undefined, 7).action, 'sleeping');
  joints.forEach((joint, i) => assert.ok(joint.position.distanceTo(sleeping[i]) < .02));
  let previous = joints.map(joint => joint.position.clone());
  const actions = new Set<string>();
  for (let frame = 0; frame < 12 * 30; frame++) {
    const clip = rig.update(1 / 30, 'idle', true, null, undefined, 8);
    actions.add(clip.action);
    joints.forEach((joint, i) => assert.ok(joint.position.distanceTo(previous[i]) < .12, `morning ${joint.name} jumps at ${frame}`));
    previous = joints.map(joint => joint.position.clone());
  }
  assert.deepEqual([...actions], ['sleeping', 'stretching', 'rest']);
});


test('rest visibly adjusts posture while remaining continuous at the seam and frozen when paused', () => {
  const rig = createOfficeCharacter('frontend', createDeskModel('frontend'));
  const hand = rig.group.getObjectByName('left-hand')!;
  const start = new THREE.Vector3();
  let previous: THREE.Vector3 | undefined, movement = 0;
  for (let frame = 0; frame < 240; frame++) {
    rig.update(1 / 30, 'idle', true, 'rest');
    if (frame === 0) start.copy(hand.position);
    movement = Math.max(movement, start.distanceTo(hand.position));
    if (previous) assert.ok(previous.distanceTo(hand.position) < .025);
    previous = hand.position.clone();
  }
  assert.ok(movement > .025);
  const paused = hand.position.clone();
  rig.update(5, 'idle', false, 'rest');
  assert.ok(hand.position.equals(paused));
});
