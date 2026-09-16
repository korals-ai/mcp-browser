// Builds the loadable extension into dist/: the service worker and the two
// pages bundled with esbuild, everything else copied. `--test` instead
// builds the pure modules as importable ESM into build/test/ for node --test.

import { build } from 'esbuild';
import { cpSync, mkdirSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const forTests = process.argv.includes('--test');

if (forTests) {
  const out = join(here, 'build', 'test');
  rmSync(out, { recursive: true, force: true });
  await build({
    entryPoints: [join(here, 'src/relayConnection.ts'), join(here, 'src/connectedTabGroup.ts'), join(here, 'src/protocol.ts')],
    bundle: true,
    format: 'esm',
    platform: 'neutral',
    target: 'es2022',
    outdir: out,
    logLevel: 'error',
  });
  console.log(`test build → ${out}`);
} else {
  const dist = join(here, 'dist');
  rmSync(dist, { recursive: true, force: true });
  mkdirSync(join(dist, 'lib', 'ui'), { recursive: true });
  await build({
    entryPoints: [join(here, 'src/background.ts')],
    bundle: true,
    format: 'esm',
    platform: 'browser',
    target: 'chrome120',
    outfile: join(dist, 'lib', 'background.js'),
    logLevel: 'error',
  });
  await build({
    entryPoints: [join(here, 'src/ui/connect.ts'), join(here, 'src/ui/status.ts')],
    bundle: true,
    format: 'esm',
    platform: 'browser',
    target: 'chrome120',
    outdir: join(dist, 'lib', 'ui'),
    logLevel: 'error',
  });
  cpSync(join(here, 'manifest.json'), join(dist, 'manifest.json'));
  cpSync(join(here, 'icons'), join(dist, 'icons'), { recursive: true });
  cpSync(join(here, 'src/ui/connect.html'), join(dist, 'connect.html'));
  cpSync(join(here, 'src/ui/status.html'), join(dist, 'status.html'));
  cpSync(join(here, 'src/ui/ui.css'), join(dist, 'lib', 'ui', 'ui.css'));
  console.log(`extension → ${dist}`);
}
