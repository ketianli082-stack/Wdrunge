import { rolldown } from 'rolldown';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const directory = await mkdtemp(join(tmpdir(), 'message-content-tests-'));
try {
  const bundle = await rolldown({
    input: fileURLToPath(new URL('./message-content.test.tsx', import.meta.url)),
    platform: 'node',
    moduleTypes: { '.css': 'empty' },
  });
  const file = join(directory, 'messages.test.mjs');
  await bundle.write({ file, format: 'esm' });
  await bundle.close();
  process.exitCode = spawnSync(process.execPath, ['--test', file], { stdio: 'inherit' }).status ?? 1;
} finally {
  await rm(directory, { recursive: true, force: true });
}
