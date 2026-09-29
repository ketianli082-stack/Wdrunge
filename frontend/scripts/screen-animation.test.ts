import test from 'node:test';
import assert from 'node:assert/strict';
import { createScreenSurface, type ScreenKind } from '../src/ScreenSurface';
import { screenMode, type ScreenActivity } from '../src/ScreenAnimations';
import type { DeskRole } from '../src/DeskScene';

const activity = (role: DeskRole, screenIndex: number, time = 3): ScreenActivity => ({ role, screenIndex, clip: { action: 'gaming', input: 'keyboard-mouse', time, duration: 12 } });

test('games are confined to the two allowed main monitors, and never render during work or failure', () => {
  for (const role of ['leader', 'product', 'designer', 'frontend', 'backend', 'testing'] as DeskRole[]) {
    for (let index = 0; index < 3; index++) {
      const context = activity(role, index);
      assert.equal(screenMode('idle', context), role === 'backend' && index === 1 ? 'arena' : role === 'testing' && index === 0 ? 'racing' : 'work');
      assert.equal(screenMode('running', context), 'work');
      assert.equal(screenMode('failed', context), 'work');
    }
  }
});

test('work screens and both games draw changing content; pause caches the exact frame and state changes clear gaming', () => {
  const previousDocument = globalThis.document;
  const operations: unknown[] = [];
  const context = new Proxy({}, {
    get: (_, name) => (...args: unknown[]) => operations.push([name, ...args]),
    set: (_, name, value) => { operations.push([name, value]); return true; },
  });
  Object.assign(globalThis, { document: { createElement: () => ({ getContext: () => context }) } });
  try {
    for (const kind of ['overview', 'roadmap', 'design', 'preview', 'code', 'terminal', 'metrics', 'checks'] as ScreenKind[]) {
      const screen = createScreenSurface(1.3, .7, kind, '#91b5a2');
      operations.length = 0; screen.update(1, 'running', true);
      const first = JSON.stringify(operations);
      operations.length = 0; screen.update(2, 'running', true);
      assert.notEqual(JSON.stringify(operations), first, `${kind} content does not move`);
      const version = screen.texture.version;
      assert.equal(screen.update(2, 'running', false), false);
      assert.equal(screen.update(20, 'running', false), false);
      assert.equal(screen.texture.version, version, `${kind} changed while paused`);
      screen.mesh.geometry.dispose(); screen.mesh.material.dispose(); screen.texture.dispose();
    }
    for (const role of ['backend', 'testing'] as const) {
      const screen = createScreenSurface(1.3, .7, role === 'backend' ? 'code' : 'checks', '#91b5a2');
      const context = activity(role, role === 'backend' ? 1 : 0);
      operations.length = 0; screen.update(3, 'idle', true, context);
      const first = JSON.stringify(operations);
      const later = { ...context, clip: { ...context.clip, time: 4 } };
      operations.length = 0; screen.update(4, 'idle', true, later);
      assert.notEqual(JSON.stringify(operations), first);
      assert.equal(screen.update(4, 'idle', false, later), false);
      assert.equal(screen.update(4, 'running', false, later), true);
      assert.equal(screen.mesh.userData.mode, 'work');
      screen.update(4, 'failed', false, later);
      assert.equal(screen.mesh.userData.mode, 'work');
      screen.mesh.geometry.dispose(); screen.mesh.material.dispose(); screen.texture.dispose();
    }
  } finally { Object.assign(globalThis, { document: previousDocument }); }
});
