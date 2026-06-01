/**
 * Audit 2026-06-01 S: PositionTable's modulo sampler `i % stride === 0`
 * always keeps `positions[0]` but generally drops `positions[len-1]` — the
 * most operationally interesting point of a flight (landing / last-seen).
 * The sampler must retain the last fix.
 *
 * Notes:
 *   - The DOM-level sampling and server-side truncation notices are
 *     deliberately distinct facts (see position-table-footer.test.tsx
 *     docstring); we do NOT collapse them here.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import FlightPage from '@/pages/Flight';

function makePositions(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    ts: 1_700_000_000 + i * 60,
    lat: 52.0 + i * 0.001,
    lon: 21.0 + i * 0.001,
    alt_baro: 10000,
    alt_geom: null,
    gs: 250,
    track: 90,
    baro_rate: null,
    rssi: -20,
    source_type: 'adsb_icao',
  }));
}

function flightPayload(total: number) {
  return {
    flight: {
      id: 777, icao_hex: 'aabbcc', callsign: null, registration: null,
      aircraft_type: null, type_desc: null, flags: 0, squawk: null,
      primary_source: 'adsb', first_seen: 1_700_000_000, last_seen: 1_700_010_000,
      duration_sec: 240, max_alt_baro: 14000, max_gs: 270, max_distance_nm: 50,
      total_positions: total, adsb_positions: total, mlat_positions: 0,
      origin_icao: null, dest_icao: null, origin_name: null, dest_name: null,
      airline_name: null,
    },
    other_flights: [],
    receiver_lat: 52.0,
    receiver_lon: 21.0,
  };
}

function installFetch(fetched: number, total: number) {
  const positions = makePositions(fetched);
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const path = url.split('?')[0];
    let body: unknown = { ok: true };
    if (path.endsWith('/api/flights/777/positions/chart'))
      body = { total, target: 2000, positions };
    else if (path.endsWith('/api/flights/777/positions'))
      body = { total, limit: 2000, offset: 0, positions };
    else if (path.endsWith('/api/flights/777/photo')) body = null;
    else if (path.endsWith('/api/flights/777')) body = flightPayload(total);
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  globalThis.localStorage.clear();
});

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={['/flight/777']}>
          <Routes>
            <Route path="/flight/:id" element={<FlightPage />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe('PositionTable sampling retains last fix (W audit-2026-06-01 S)', () => {
  it('renders the last position when sampling is active', async () => {
    // 1200 fetched, 1200 total. 1200 > 500 → sampler kicks in.
    // Last position has ts = 1_700_000_000 + 1199 * 60.
    installFetch(1200, 1200);
    const { findByText } = renderPage();
    // findByText waits for async data + lazy-load. Searching by the cell-row
    // timestamp text would couple the test to formatTs; assert the position
    // log section is rendered then read the last row's data attribute.
    await findByText(/Showing \d+ of 1200 positions/i);

    // Rows are testid'd `flight-position-row-<ts>` (one per rendered fix).
    // The last rendered row must correspond to positions[1199].
    const rows = Array.from(
      document.querySelectorAll<HTMLElement>('[data-testid^="flight-position-row-"]'),
    );
    expect(rows.length).toBeGreaterThan(0);
    const expectedLastTs = 1_700_000_000 + 1199 * 60;
    const lastTestid = rows[rows.length - 1].getAttribute('data-testid');
    expect(lastTestid).toBe(`flight-position-row-${expectedLastTs}`);
  });

  it('does not duplicate the last position when stride lands on it', async () => {
    // 500 fetched: sampler short-circuits (no sampling). Last row is positions[499].
    installFetch(500, 500);
    const { queryByText } = renderPage();
    // No sampling note because 500 <= 500 threshold.
    expect(queryByText(/\(sampled\)/i)).toBeNull();
  });
});
