/**
 * Code-review fix #1 — the position-log footer must not divide the
 * downsampled row count by the server-side total. `/positions` is fetched
 * with `?limit=2000`, so `positions` is already capped; the table then
 * samples that page down to ≤500 rows for the DOM. Two distinct facts:
 *   - sampling:   rendered < fetched  → "Showing X of <fetched> (sampled)"
 *   - truncation: fetched  < total    → "first <fetched> of <total> fixes"
 * The old single line "Showing <sampled> of <total> (sampled)" conflated
 * both and showed a denominator (total) the table never sampled from.
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
      id: 555, icao_hex: 'aabbcc', callsign: null, registration: null,
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
    if (path.endsWith('/api/flights/555/positions/chart'))
      body = { total, target: 2000, positions };
    else if (path.endsWith('/api/flights/555/positions'))
      body = { total, limit: 2000, offset: 0, positions };
    else if (path.endsWith('/api/flights/555/photo')) body = null;
    else if (path.endsWith('/api/flights/555')) body = flightPayload(total);
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
        <MemoryRouter initialEntries={['/flight/555']}>
          <Routes>
            <Route path="/flight/:id" element={<FlightPage />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe('Position log footer (review fix #1)', () => {
  it('truncation note divides by fetched count, not server total', async () => {
    // Fetched 5 rows (no sampling: ≤500) but server has 5000 total.
    installFetch(5, 5000);
    const { findByText, queryByText } = renderPage();
    // Truncation note: fetched (5) < total (5000).
    await findByText(/first 5 of 5000 .*fixes/i);
    // The misleading "of 5000 ... (sampled)" line must be gone — nothing
    // was sampled (5 ≤ 500), and 5000 is not a count the table sampled from.
    expect(queryByText(/of 5000 positions \(sampled\)/i)).toBeNull();
  });

  it('sampling note divides rendered by fetched, and no truncation note when fetched == total', async () => {
    // 600 fetched == total. 600 > 500 → sampled every-2nd = 300 picks, plus
    // the last fix (Audit 2026-06-01 S: PositionTable retains positions[len-1]
    // when the modulo sampler misses it). Net: 301 sampled rows.
    installFetch(600, 600);
    const { findByText, queryByText } = renderPage();
    await findByText(/Showing 301 of 600 positions \(sampled\)/i);
    // fetched == total → no truncation note.
    expect(queryByText(/fixes/i)).toBeNull();
  });
});
