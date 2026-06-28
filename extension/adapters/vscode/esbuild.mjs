// Bundles the React webview app (src/ui/index.tsx) into out/ui/main.js (+ .css).
// The extension host code is compiled separately by tsc (build:host). esbuild
// transpiles TSX without type-checking, so the UI builds even while types are
// still being tightened.

import * as esbuild from 'esbuild';

const watch = process.argv.includes('--watch');

const options = {
  entryPoints: ['src/ui/index.tsx'],
  bundle: true,
  outfile: 'out/ui/main.js',
  format: 'iife',
  platform: 'browser',
  target: 'es2020',
  jsx: 'automatic',
  loader: { '.css': 'css' },
  minify: !watch,
  sourcemap: watch,
  logLevel: 'info',
};

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  console.log('[esbuild] watching src/ui ...');
} else {
  await esbuild.build(options);
  console.log('[esbuild] built out/ui/main.js');
}
