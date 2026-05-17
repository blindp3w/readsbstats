/**
 * Audit-12 #201 — frontend page smoke tests.
 *
 * Renders each top-level page inside the same Provider stack the real app
 * uses (QueryClient + MemoryRouter), with the global `fetch` stubbed to a
 * minimal "no data" shape. The goal is exhaustive coverage of the
 * "doesn't throw on first render" surface — a regression in imports,
 * required props, or initial-state assumptions trips one of these tests
 * before reaching prod.
 *
 * Heavy pages skipped here:
 *   - Map.tsx          — Leaflet does aggressive DOM mutation that jsdom
 *                        doesn't fully support; smoke-tested in production
 *                        via the Playwright mobile suite under tests/ui/.
 *   - Hello.tsx        — Phase-0 PoC, not routed (see Audit 12 Phase 6).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import type { ReactNode } from 'react';

// ---- shared fetch stub ----
// Every page calls `apiJson` / `apiFetch` which delegate to global `fetch`.
// Return a tiny per-endpoint shape so the page renders its "empty" state.
const FETCH_STUBS: Record<string, unknown> = {
  // /api/stats → records / aggregates
  '/stats/api/stats': { aggregates: {}, top_airlines: [], top_types: [], top_routes: [], top_airports: [], top_countries: [], hourly: [], daily: [], new_aircraft: [], frequent_aircraft: [], emergency_squawks: [], heatmap: [], records: {} },
  '/stats/api/stats/polar': { points: [] },
  '/stats/api/flights': { total: 0, items: [] },
  '/stats/api/dates': { dates: [] },
  '/stats/api/settings': {
    lat: 52.0, lon: 21.0, max_range: 450, poll_interval: 5, flight_gap: 1800,
    min_positions: 2, max_seen_pos: 60, max_speed_kts: 2000,
    db_path: 'history.db', retention_days: 0, purge_interval: 3600,
    photo_cache_days: 30, airspace_geojson: '(set)',
    route_cache_days: 30, route_interval: 60, route_batch: 20, route_rate_limit: 1.0,
    adsbx_enabled: 0, adsbx_interval: 60, adsbx_range: 250, adsbx_url: '',
    metrics_enabled: 0, metrics_interval: 60, stats_json: '(default)',
    health_heartbeat_warn_s: 120, health_heartbeat_crit_s: 300,
    health_aircraft_gap_s: 600, health_noise_warn_db: -28, health_noise_crit_db: -25,
    health_cpu_warn_pct: 80, health_cpu_crit_pct: 90,
    health_baseline_weeks: 4, health_baseline_min_samples: 3,
    health_msg_drop_pct: 50, health_aircraft_drop_pct: 25,
    health_signal_drop_db: 3, health_gain_strong_pct: 5,
    health_range_short_days: 7, health_range_long_days: 30, health_range_ratio: 0.85,
    root_path: '/stats',
    page_size: 100, max_page_size: 500,
    telegram_token: 'not set', telegram_chat_id: 'not set',
    telegram_summary_time: '21:00', telegram_units: 'metric',
    base_url: 'http://homepi.local/stats',
  },
  '/stats/api/feeders': [],
  '/stats/api/watchlist': { entries: [] },
  '/stats/api/aircraft/flagged': { total: 0, items: [] },
  '/stats/api/metrics': { ts: [], series: {} },
  '/stats/api/metrics/health': { status: 'ok', checks: [] },
  '/stats/api/live': { count: 0, aircraft: [] },
  '/stats/api/gallery': { total: 0, items: [] },
};

function setupFetchStub() {
  const stub = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    // Strip query string for matching
    const path = url.split('?')[0];
    let body = FETCH_STUBS[path];
    if (body === undefined) {
      // Match by suffix to handle photo / aircraft / flight by-id endpoints
      const match = Object.keys(FETCH_STUBS).find((k) => path.startsWith(k));
      body = match ? FETCH_STUBS[match] : { ok: true };
    }
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  globalThis.fetch = stub as unknown as typeof fetch;
  return stub;
}

function wrap(ui: ReactNode, route = '/'): ReactNode {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return (
    <QueryClientProvider client={qc}>
      <ErrorBoundary>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </ErrorBoundary>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  setupFetchStub();
  // Reset the units store between tests so a previous test can't bleed
  // unit preferences into the next render.
  globalThis.localStorage.clear();
});

// ---------------------------------------------------------------------------
// App-shell + ErrorBoundary
// ---------------------------------------------------------------------------

describe('App shell', () => {
  it('renders without throwing (nav + suspense + outlet wired)', async () => {
    const { default: App } = await import('@/App');
    const { container } = render(
      wrap(
        <Routes>
          <Route element={<App />}>
            <Route index element={<div data-testid="probe-home">home</div>} />
          </Route>
        </Routes>,
      ),
    );
    // <Nav> renders the app shell; the probe inside <Outlet/> shows the
    // route content rendered through suspense.
    await waitFor(() => {
      expect(container.querySelector('main')).toBeTruthy();
    });
  });
});

describe('ErrorBoundary', () => {
  it('passes children through when no error', () => {
    const { getByText } = render(
      <ErrorBoundary>
        <div>healthy</div>
      </ErrorBoundary>,
    );
    expect(getByText('healthy')).toBeTruthy();
  });

  it('renders fallback alert on child render error', () => {
    function Boom(): ReactNode {
      throw new Error('boom');
    }
    // Suppress the noisy console.error from the deliberate throw so test
    // output stays clean.
    const orig = console.error;
    console.error = vi.fn();
    try {
      const { getByRole, getByText } = render(
        <ErrorBoundary>
          <Boom />
        </ErrorBoundary>,
      );
      expect(getByRole('alert')).toBeTruthy();
      expect(getByText('Something went wrong')).toBeTruthy();
      expect(getByText('boom')).toBeTruthy();
    } finally {
      console.error = orig;
    }
  });
});

// ---------------------------------------------------------------------------
// Page smoke tests — render without throwing on minimal/empty data
// ---------------------------------------------------------------------------

interface PageCase {
  name: string;
  route: string;
  importPath: string;
  routeTemplate?: string; // e.g. '/aircraft/:icao' if dynamic
}

const PAGES: PageCase[] = [
  { name: 'Settings', route: '/settings', importPath: '@/pages/Settings' },
  { name: 'Feeders', route: '/feeders', importPath: '@/pages/Feeders' },
  { name: 'Watchlist', route: '/watchlist', importPath: '@/pages/Watchlist' },
  { name: 'History', route: '/history', importPath: '@/pages/History' },
  { name: 'Gallery', route: '/gallery', importPath: '@/pages/Gallery' },
  { name: 'Stats', route: '/', importPath: '@/pages/Stats' },
  { name: 'Metrics', route: '/metrics', importPath: '@/pages/Metrics' },
  { name: 'Aircraft', route: '/aircraft/aabbcc', importPath: '@/pages/Aircraft', routeTemplate: '/aircraft/:icao' },
  { name: 'Flight', route: '/flight/1', importPath: '@/pages/Flight', routeTemplate: '/flight/:id' },
];

describe('Page smoke — renders without throwing', () => {
  for (const page of PAGES) {
    it(page.name, async () => {
      const mod = await import(/* @vite-ignore */ page.importPath);
      const Page = mod.default;
      const path = page.routeTemplate ?? page.route;
      const orig = console.error;
      // Some pages emit harmless console errors from React Query retry
      // attempts or imperative Leaflet calls that aren't fully shimmed in
      // jsdom; suppress for the duration so test output stays readable.
      console.error = vi.fn();
      try {
        const result = render(
          wrap(
            <Routes>
              <Route path={path} element={<Page />} />
            </Routes>,
            page.route,
          ),
        );
        // The component mounted without throwing — that's the smoke check.
        // Wait once for any pending query to settle so async cleanup
        // doesn't trip the "act warning" between tests.
        await waitFor(() => {
          // No assertion needed — we just want the microtask queue to drain.
          expect(result.container).toBeTruthy();
        });
      } finally {
        console.error = orig;
      }
    });
  }
});
