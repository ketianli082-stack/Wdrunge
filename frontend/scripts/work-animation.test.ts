import test from 'node:test';
import assert from 'node:assert/strict';
import { advanceWork, buildSeconds, workCodeLine, workPage } from '../src/WorkAnimation';
import { drawWorkAnimation } from '../src/ScreenAnimations';

test('editing progress continues across work actions and pauses during mouse use and leisure', () => {
  const progress = { elapsed: 0, editing: 0 };
  advanceWork(progress, 2, true, 'keyboard');
  advanceWork(progress, 6, true, 'mouse');
  assert.deepEqual(progress, { elapsed: 8, editing: 2 });
  advanceWork(progress, 5, false, 'keyboard-mouse');
  advanceWork(progress, 0, true, 'keyboard');
  assert.deepEqual(progress, { elapsed: 8, editing: 2 });
  advanceWork(progress, 3, true, 'keyboard');
  assert.deepEqual(progress, { elapsed: 11, editing: 5 });
});

test('frontend code describes the exact title, theme and column count applied by the next preview build', () => {
  for (let build = 0; build < 8; build++) {
    const next = workPage((build + 1) * buildSeconds);
    assert.ok(workCodeLine(build * 6 + 1, 'frontend').includes(next.title));
    assert.ok(workCodeLine(build * 6 + 2, 'frontend').includes(next.color));
    assert.ok(workCodeLine(build * 6 + 3, 'frontend').includes(String(next.columns)));
    assert.equal(workPage((build + 1) * buildSeconds - .001).revision, build);
  }
});

test('code keeps scrolling past the old nine-second reset, with matching frontend preview revisions', () => {
  const texts: string[] = [];
  const context = new Proxy({}, { get: (_, name) => (...args: unknown[]) => { if (name === 'fillText') texts.push(String(args[0])); }, set: () => true }) as CanvasRenderingContext2D;
  for (const editing of [8.9, 9.1, 20, 31]) {
    const activity = { role: 'frontend' as const, screenIndex: 0, clip: { action: 'typing' as const, input: 'keyboard' as const, time: 2, duration: 10 }, work: { elapsed: editing, editing } };
    texts.length = 0;
    drawWorkAnimation(context, 'code', editing, activity);
    const lineNumbers = texts.filter(value => /^\d+$/.test(value)).map(Number);
    assert.ok(Math.max(...lineNumbers) >= Math.floor(editing / .8) + 1);
    assert.ok(Math.min(...lineNumbers) > 1);
    texts.length = 0;
    drawWorkAnimation(context, 'preview', editing, activity);
    const page = workPage(editing);
    assert.ok(texts.includes(page.title));
    assert.ok(texts.some(value => value.includes(`v${page.revision}`)));
  }
});
