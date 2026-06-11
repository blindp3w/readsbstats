import { defineConfig } from 'vitest/config';
import react, { reactCompilerPreset } from '@vitejs/plugin-react';
import babel from '@rolldown/plugin-babel';
import path from 'node:path';

export default defineConfig({
  plugins: [react(), babel({ presets: [reactCompilerPreset()] })],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  // Stub the build-time constants used by the Settings build-info card.
  // Vite injects these via `define` in vite.config.ts during real builds;
  // under vitest they are otherwise ReferenceErrors at render time.
  define: {
    __APP_VERSION__: JSON.stringify('test'),
    __FRONTEND_BUILD__: JSON.stringify('test-build'),
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./test/setup.ts'],
    css: false,
    coverage: {
      provider: 'v8',
      reportsDirectory: './coverage',
      // Vitest 4 removed `coverage.all`; without an explicit include, files
      // never imported by any test are invisible in the report — which would
      // hide exactly the untested components the report exists to surface.
      include: ['src/**/*.{ts,tsx}'],
    },
  },
});
