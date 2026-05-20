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
// - React Compiler 1.0 is enabled via Babel plugin (auto-memoization).
// - Manual chunks split Leaflet (~140 KB raw) and ECharts out of the shell
//   so pages that don't use them (settings, watchlist, feeders) don't pay
//   the bytes on first paint.
// - Sourcemaps are OFF — the repo is public and source maps embed absolute
//   developer paths. For one-off prod debugging, set `sourcemap: 'hidden'`
//   temporarily and don't commit the change.
// - `ANALYZE=1 npm run build` emits dist/stats.html for bundle inspection.
export default defineConfig(({ command }) => ({
  plugins: [
    react({
      babel: { plugins: [['babel-plugin-react-compiler', {}]] },
    }),
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
    chunkSizeWarningLimit: 250,
    rollupOptions: {
      output: {
        manualChunks: {
          leaflet: ['leaflet', 'react-leaflet'],
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
        },
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
