/**
 * v2.9.0 M3.3 — Position-log RSSI cell uses a RELATIVE bar (green >
 * flight median, amber ≤). Text is dim. Previous absolute-threshold
 * coloring (`rssiColor()`) is gone. Plus the per-row source stripe on
 * the first cell, and inline iPhone disclosure on row tap.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import FlightPage from '@/pages/Flight';

// 5 positions with deliberate RSSI spread:
//   -50 (min, < median)
//   -30 (< median)
//   -20 (= median)
//   -10 (> median)
//   -3  (max, > median)
const positions = [-50, -30, -20, -10, -3].map((rssi, i) => ({
  ts: 1_700_000_000 + i * 60,
  lat: 52.0 + i * 0.01,
  lon: 21.0 + i * 0.01,
  alt_baro: 10000 + i * 1000,
  alt_geom: null,
  gs: 250 + i * 5,
  track: 90,
  baro_rate: null,
  rssi,
  source_type: i % 2 === 0 ? 'adsb_icao' : 'mlat',
}));

const PAYLOAD = {
  flight: {
    id: 555,
    icao_hex: 'aabbcc',
    callsign: null,
    registration: null,
    aircraft_type: null,
    type_desc: null,
    flags: 0,
    squawk: null,
    primary_source: 'adsb',
    first_seen: positions[0].ts,
    last_seen: positions[positions.length - 1].ts,
    duration_sec: 240,
    max_alt_baro: 14000,
    max_gs: 270,
    max_distance_nm: 50,
    total_positions: 5,
    adsb_positions: 3,
    mlat_positions: 2,
    origin_icao: null,
    dest_icao: null,
    origin_name: null,
    dest_name: null,
    airline_name: null,
  },
  other_flights: [],
  receiver_lat: 52.0,
  receiver_lon: 21.0,
};

// BE-10/FE-1: the position-log table reads the paginated /positions
// endpoint, not the (now-empty) embedded detail list.
beforeEach(() => {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const path = url.split('?')[0];
    let body: unknown = { ok: true };
    if (path.endsWith('/api/flights/555/positions/chart'))
      body = { total: positions.length, target: 2000, positions };
    else if (path.endsWith('/api/flights/555/positions'))
      body = { total: positions.length, limit: 2000, offset: 0, positions };
    else if (path.endsWith('/api/flights/555/photo')) body = null;
    else if (path.endsWith('/api/flights/555')) body = PAYLOAD;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
  globalThis.localStorage.clear();
  // useIsMobile gates row-tap disclosure. Default matchMedia stub in
  // test/setup.ts returns matches:false (desktop); override to true so
  // the click→expand path is exercised.
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: true,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
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

describe('Position log RSSI cell (M3.3)', () => {
  it('renders one RssiCell per row with the right data-strong attr', async () => {
    const { container } = renderPage();
    await waitFor(() => {
      const cells = container.querySelectorAll('[data-testid="rssi-cell"]');
      if (cells.length === 0) throw new Error('rssi cells not ready');
    });
    const cells = container.querySelectorAll('[data-testid="rssi-cell"]');
    expect(cells.length).toBe(5);
    // Median is -20 → strict > median means -10 and -3 are strong; the
    // others (-50, -30, -20) are not.
    const strong = Array.from(cells).map((c) => c.getAttribute('data-strong'));
    expect(strong).toEqual(['false', 'false', 'false', 'true', 'true']);
  });

  it('per-row stripe is on the first cell, colored by source_type', async () => {
    const { container } = renderPage();
    await waitFor(() => {
      const rows = container.querySelectorAll('[data-testid^="flight-position-row-"]');
      if (rows.length === 0) throw new Error('rows not ready');
    });
    const rows = container.querySelectorAll('[data-testid^="flight-position-row-"]');
    // Row 0 is adsb_icao → success (green) stripe.
    const firstCell0 = rows[0].querySelector('td');
    expect(firstCell0?.style.borderLeftColor).toBe('var(--color-success)');
    // Row 1 is mlat → warn (amber).
    const firstCell1 = rows[1].querySelector('td');
    expect(firstCell1?.style.borderLeftColor).toBe('var(--color-warn)');
  });

  it('clicking a row toggles its inline disclosure detail row', async () => {
    const { container } = renderPage();
    await waitFor(() => {
      if (!container.querySelector('[data-testid="flight-position-row-1700000000"]'))
        throw new Error('row not ready');
    });
    const row = container.querySelector(
      '[data-testid="flight-position-row-1700000000"]',
    ) as HTMLElement;
    // Detail row hidden by default.
    expect(
      container.querySelector('[data-testid="flight-position-detail-1700000000"]'),
    ).toBeNull();
    fireEvent.click(row);
    await waitFor(() => {
      if (!container.querySelector('[data-testid="flight-position-detail-1700000000"]'))
        throw new Error('detail not opened');
    });
    fireEvent.click(row);
    await waitFor(() => {
      if (container.querySelector('[data-testid="flight-position-detail-1700000000"]'))
        throw new Error('detail did not close');
    });
  });
});
