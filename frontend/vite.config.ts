import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { visualizer } from 'rollup-plugin-visualizer';
import path from 'node:path';
import fs from 'node:fs';
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
// - React Compiler integration: @vitejs/plugin-react@6 dropped the inline
//   `babel.plugins` escape hatch (oxc/rolldown by default in vite 8). The
//   replacement path is the `reactCompilerPreset` export wired through
//   @rolldown/plugin-babel. Tracked as a follow-up; the compiler's value
//   is purely automatic memoisation, so its absence is a perf regression
//   on chart-heavy pages but not a correctness change.
//
// Manual-chunks declared as a function (Rollup 4 tightened the type from
// dict-or-function to function-only). Same matching semantics — split by
// substring against the resolved module id.
const _MANUAL_CHUNK_GROUPS: Record<string, readonly string[]> = {
  maps: ['maplibre-gl', '@vis.gl/react-maplibre', 'react-map-gl'],
  charts: ['echarts/core', 'echarts/charts', 'echarts/components', 'echarts/renderers'],
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
    '@radix-ui/react-slot',
  ],
};

function chunkFor(id: string): string | undefined {
  for (const [chunk, packages] of Object.entries(_MANUAL_CHUNK_GROUPS)) {
    if (packages.some((pkg) => id.includes(`/node_modules/${pkg}/`))) {
      return chunk;
    }
  }
  return undefined;
}

export default defineConfig(({ command }) => ({
  plugins: [
    react(),
    tailwindcss(),
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
