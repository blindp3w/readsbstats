import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import StatsPage from '@/pages/Stats';

// Minimal stats payload covering everything Stats.tsx reads on mount.
const STATS_PAYLOAD = {
  total_flights: 42,
  total_positions: 1000,
  unique_aircraft: 30,
  unique_airlines: 5,
  db_size_bytes: 1234,
  oldest_flight: 1_700_000_000,
  flights_last_24h: 12,
  flights_last_7d: 80,
  source_breakdown: { adsb: 80, mlat: 18, other: 2 },
  top_aircraft_types: [],
  top_airlines: [],
  top_countries: [],
  frequent_aircraft: [],
  top_routes: [],
  top_airports: [],
  hourly_distribution: [],
  daily_unique_aircraft: [],
  altitude_distribution: [],
  military_flights: 0,
  interesting_flights: 0,
  anonymous_flights: 0,
  heatmap: [],
  squawk_counts: {},
  furthest_aircraft: null,
  lifetime: {
    total_flights: 0,
    total_positions: 0,
    unique_aircraft: 0,
    unique_airlines: 0,
    oldest_flight: null,
    db_size_bytes: null,
    source_breakdown: { adsb: 0, mlat: 0, other: 0 },
  },
};

beforeEach(() => {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const path = url.split('?')[0];
    let body: unknown = { ok: true };
    if (path.endsWith('/api/stats')) body = STATS_PAYLOAD;
    else if (path.endsWith('/api/stats/polar')) body = { buckets: [] };
    else if (path.endsWith('/api/stats/records'))
      body = { fastest: null, furthest: null, highest: null, longest: null };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
  globalThis.localStorage.clear();
});

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={['/']}>
          <Routes>
            <Route path="/" element={<StatsPage />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe('Stats page layout', () => {
  it('renders the four section landmarks', async () => {
    const { container } = renderPage();
    await waitFor(() => {
      expect(container.querySelector('#overview')).not.toBeNull();
      expect(container.querySelector('#activity')).not.toBeNull();
      expect(container.querySelector('#rankings')).not.toBeNull();
      expect(container.querySelector('#coverage')).not.toBeNull();
    });
  });

  it('renders the four KPI cards', async () => {
    const { getByTestId } = renderPage();
    await waitFor(() => {
      expect(getByTestId('kpi-flights')).toBeTruthy();
      expect(getByTestId('kpi-unique-aircraft')).toBeTruthy();
      expect(getByTestId('kpi-positions')).toBeTruthy();
      expect(getByTestId('kpi-max-range')).toBeTruthy();
    });
  });

  it('renders the flag strip and About footer', async () => {
    const { getByTestId } = renderPage();
    await waitFor(() => {
      expect(getByTestId('stats-flag-strip')).toBeTruthy();
      expect(getByTestId('stats-about-receiver')).toBeTruthy();
    });
  });

  it('renders the sticky range picker wrapper', async () => {
    const { getByTestId } = renderPage();
    await waitFor(() => {
      expect(getByTestId('range-picker-sticky')).toBeTruthy();
    });
  });
});
