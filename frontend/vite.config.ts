import { defineConfig, type Plugin } from 'vite';
import react, { reactCompilerPreset } from '@vitejs/plugin-react';
import babel from '@rolldown/plugin-babel';
import tailwindcss from '@tailwindcss/vite';
import { visualizer } from 'rollup-plugin-visualizer';
import path from 'node:path';
import fs from 'node:fs';
import zlib from 'node:zlib';
import { execSync } from 'node:child_process';

function getFrontendBuild(): string {
  try {
    const sha = execSync('git rev-parse --short HEAD', { encoding: 'utf8' }).trim();
    const date = new Date().toISOString().slice(0, 10);
    return `${sha} · ${date}`;
  } catch {
    return 'unknown';
  }
}

function getAppVersion(): string {
  // Single source of truth: `pyproject.toml`. Cheap regex parse — pulling in
  // a TOML library just for one field would inflate the dev dep surface for
  // no real benefit.
  try {
    const toml = fs.readFileSync(path.resolve(__dirname, '..', 'pyproject.toml'), 'utf8');
    const m = toml.match(/^version\s*=\s*"([^"]+)"/m);
    return m ? m[1] : 'unknown';
  } catch {
    return 'unknown';
  }
}

// Vite build config for the readsbstats v2 SPA.
//
// - `base` differs between dev and prod: dev is root-mounted (Vite at :5173,
//   no nginx); prod is mounted under /stats/v2/ behind nginx → uvicorn.
// - Manual chunks split the map stack (MapLibre GL + react-map-gl) and
//   ECharts out of the shell so pages that don't use them (settings,
//   watchlist, feeders) don't pay the bytes on first paint.
// - Sourcemaps are OFF — the repo is public and source maps embed absolute
//   developer paths. For one-off prod debugging, set `sourcemap: 'hidden'`
//   temporarily and don't commit the change.
// - `ANALYZE=1 npm run build` emits dist/stats.html for bundle inspection.
// - React Compiler: @vitejs/plugin-react@6 uses Oxc (no Babel). Wire the
//   compiler via `reactCompilerPreset` + `@rolldown/plugin-babel` — the
//   explicit opt-in path documented for v6+.
//
// Manual-chunks declared as a function (Rollup 4 tightened the type from
// dict-or-function to function-only). Same matching semantics — split by
// substring against the resolved module id.
const _MANUAL_CHUNK_GROUPS: Record<string, readonly string[]> = {
  maps: ['maplibre-gl', '@vis.gl/react-maplibre', 'react-map-gl'],
  // echarts v6 ships `core.js`/`charts.js`/`components.js`/`renderers.js` as
  // top-level *files* (not directories), so the old `echarts/core` +
  // trailing-slash matcher in chunkFor never matched and echarts silently fell
  // into the importing component chunk. Match the whole `echarts` package (its
  // internals live under `echarts/lib/`) plus its `zrender` renderer dep so the
  // chart stack groups into one deterministic `charts` chunk again.
  charts: ['echarts', 'zrender'],
  vendor: ['react', 'react-dom', 'react-router-dom'],
  // Radix primitives are 25-30 KB gz total — isolate so settings/
  // history/feeders pages that don't open dialogs/dropdowns still
  // see them under modulepreload, but the shell stays lean.
  radix: [
    '@radix-ui/react-select',
    '@radix-ui/react-dialog',
    '@radix-ui/react-popover',
    '@radix-ui/react-toggle-group',
    '@radix-ui/react-dropdown-menu',
    '@radix-ui/react-tooltip',
  ],
  // ACARS message decoder (35.5 KB gz + pako). Lazy-loaded only on VDL2 surfaces
  // (feed page + flight ACARS panel) via hooks/useAcarsDecoder — keep it out of
  // the shell. Budgeted in _CHUNK_BUDGETS_GZ_KB once a component imports it.
  'vdl2-decoder': ['@airframes/acars-decoder', 'pako'],
};

function chunkFor(id: string): string | undefined {
  for (const [chunk, packages] of Object.entries(_MANUAL_CHUNK_GROUPS)) {
    if (packages.some((pkg) => id.includes(`/node_modules/${pkg}/`))) {
      return chunk;
    }
  }
  return undefined;
}

// FE-3 — hard budget on the two heavy lazy chunks (gzipped KB, i.e. what the
// Pi-4 actually ships over the wire). `maps` and `charts` are code-split behind
// page-level dynamic imports (/map + /flight, stats/metrics/flight) and must
// never load on first paint. The budget fails the build on two regressions:
//   (a) an accidental eager import pulls maplibre/echarts into the shell — the
//       named chunk shrinks or vanishes (caught by the "missing" check), and
//   (b) a dependency bump bloats the chunk past its ceiling.
// Ceilings sit ~15-20% above current size so routine patch bumps don't trip it
// but a structural regression does. Update deliberately when a dep grows for a
// real reason.
const _CHUNK_BUDGETS_GZ_KB: Record<string, number> = {
  maps: 340,
  charts: 230,
  'vdl2-decoder': 45,  // ~39 KB gz (decoder + pako); ceiling ~15% above current
};

function bundleBudget(): Plugin {
  return {
    name: 'rsbs-bundle-budget',
    generateBundle(_options, bundle) {
      const violations: string[] = [];
      const seen = new Set<string>();
      for (const file of Object.values(bundle)) {
        if (file.type !== 'chunk') continue;
        const budget = _CHUNK_BUDGETS_GZ_KB[file.name];
        if (budget === undefined) continue;
        seen.add(file.name);
        const gzKb = zlib.gzipSync(file.code).length / 1024;
        if (gzKb > budget) {
          violations.push(`  ${file.name}: ${gzKb.toFixed(1)} KB gz > budget ${budget} KB`);
        }
      }
      for (const name of Object.keys(_CHUNK_BUDGETS_GZ_KB)) {
        if (!seen.has(name)) {
          violations.push(`  ${name}: chunk missing — likely eagerly imported into the shell`);
        }
      }
      if (violations.length > 0) {
        this.error(`bundle-size budget exceeded:\n${violations.join('\n')}`);
      }
    },
  };
}

export default defineConfig(({ command }) => ({
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset()] }),
    tailwindcss(),
    command === 'build' ? bundleBudget() : null,
    process.env.ANALYZE
      ? visualizer({ filename: 'dist/stats.html', gzipSize: true, brotliSize: true })
      : null,
  ].filter(Boolean),
  // Production base = '/stats/' (nginx subpath). Dev base = '/' (root).
  // Was '/stats/v2/' during the v1 coexistence period; flipped to '/stats/'
  // at v2.0.0 cutover when the Jinja UI was deleted.
  base: command === 'build' ? '/stats/' : '/',
  define: {
    __FRONTEND_BUILD__: JSON.stringify(getFrontendBuild()),
    __APP_VERSION__: JSON.stringify(getAppVersion()),
  },
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false,
    // Bumped from 600 → 1500 (KB raw) at v2.4.0 — two lazy-loaded chunks
    // legitimately exceed the default: `charts` (ECharts core, 572 KB raw /
    // 192 KB gz, since v2.2.0) and `maps` (maplibre-gl + react-map-gl, ~1.2
    // MB raw / 327 KB gz). Both are gated behind page-specific dynamic
    // imports and never load on Stats/History/Settings/Watchlist/Feeders/
    // Gallery first paint. The warning's signal is gone for these two.
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        manualChunks: chunkFor,
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: false },
    },
  },
}));
