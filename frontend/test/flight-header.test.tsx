/**
 * v2.9.0 M3.1 — Flight detail compact header. Tests pin:
 *   - Identity row contains reg / callsign / hex / squawk / source badge
 *   - Subtitle line joins aircraft type / desc / operator / route
 *   - Metric grid has 4 cells with correct labels + values + sublabels
 *   - At-max sublabels render when positions provide the data, render
 *     nothing (sublabel omitted) when positions are empty
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import FlightPage from '@/pages/Flight';

const FLIGHT_PAYLOAD = {
  flight: {
    id: 12345,
    icao_hex: 'aabbcc',
    callsign: 'LO281',
    registration: 'SP-LRF',
    aircraft_type: 'B789',
    type_desc: 'Boeing 787-9',
    flags: 0,
    squawk: '1234',
    primary_source: 'adsb',
    first_seen: 1_700_000_000,
    last_seen: 1_700_003_600,
    duration_sec: 3600,
    max_alt_baro: 41000,
    max_gs: 521,
    max_distance_nm: 312,
    total_positions: 250,
    adsb_positions: 240,
    mlat_positions: 10,
    origin_icao: 'WAW',
    dest_icao: 'JFK',
    origin_name: 'Warsaw',
    dest_name: 'New York JFK',
    airline_name: 'LOT Polish Airlines',
  },
  other_flights: [],
  receiver_lat: 52.0,
  receiver_lon: 21.0,
};

// BE-10/FE-1: the at-max sublabels now derive from the downsampled
// /positions/chart series (target=2000), NOT the detail payload — the
// detail endpoint no longer embeds positions. These rows feed the chart
// endpoint mock; the detail payload deliberately carries no positions key.
const CHART_POSITIONS = [
  {
    ts: 1_700_000_000,
    lat: 52.1,
    lon: 21.0,
    alt_baro: 1000,
    alt_geom: null,
    gs: 200,
    track: 90,
    baro_rate: 2000,
    rssi: -10,
    source_type: 'adsb_icao',
  },
  {
    ts: 1_700_001_800,
    lat: 52.5,
    lon: 22.0,
    alt_baro: 41000, // max
    alt_geom: null,
    gs: 521, // max
    track: 285,
    baro_rate: -2400,
    rssi: -8,
    source_type: 'adsb_icao',
  },
  {
    ts: 1_700_003_600,
    lat: 53.0,
    lon: 25.0, // farthest from receiver (52, 21)
    alt_baro: 39000,
    alt_geom: null,
    gs: 480,
    track: 270,
    baro_rate: -1500,
    rssi: -6,
    source_type: 'adsb_icao',
  },
];

beforeEach(() => {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const path = url.split('?')[0];
    let body: unknown = { ok: true };
    if (path.endsWith('/api/flights/12345/positions/chart'))
      body = { total: CHART_POSITIONS.length, target: 2000, positions: CHART_POSITIONS };
    else if (path.endsWith('/api/flights/12345/positions'))
      body = {
        total: CHART_POSITIONS.length,
        limit: 2000,
        offset: 0,
        positions: CHART_POSITIONS,
      };
    else if (path.endsWith('/api/flights/12345/photo')) body = null;
    else if (path.endsWith('/api/flights/12345')) body = FLIGHT_PAYLOAD;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
  globalThis.localStorage.clear();
});

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={['/flight/12345']}>
          <Routes>
            <Route path="/flight/:id" element={<FlightPage />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe('Flight detail split endpoints (BE-10 / FE-1)', () => {
  it('pulls positions from the split endpoints, never the embedded list', async () => {
    // FLIGHT_PAYLOAD carries NO `positions` key. The page must still
    // render the position-log count and the at-max sublabels by fetching
    // /positions and /positions/chart respectively.
    const { container } = renderPage();
    await waitFor(() => {
      if (!container.querySelector('[data-testid="flight-positions-card"]'))
        throw new Error('positions card not ready');
    });
    const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls;
    const urls = calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes('/positions/chart?target=2000'))).toBe(true);
    // audit 2026-06-15: the profile chart now reuses the target=2000 series
    // (ECharts lttb-samples it at render), so the redundant target=500 fetch
    // is gone — one fewer request per flight view.
    expect(urls.some((u) => u.includes('/positions/chart?target=500'))).toBe(false);
    expect(urls.some((u) => /\/positions\?limit=\d+/.test(u))).toBe(true);
    // Count derives from the /positions `total`, not an embedded array.
    const card = container.querySelector('[data-testid="flight-positions-card"]')!;
    expect(card.textContent).toContain('Position log (3)');
  });
});

describe('Flight detail compact header (M3.1)', () => {
  it('renders identity row with reg, callsign, hex, squawk, source badge', async () => {
    const { container } = renderPage();
    await waitFor(() => {
      if (!container.querySelector('[data-testid="flight-identity"]'))
        throw new Error('identity not ready');
    });
    const identity = container.querySelector('[data-testid="flight-identity"]')!;
    const text = identity.textContent ?? '';
    expect(text).toContain('SP-LRF');
    expect(text).toContain('LO281');
    expect(text).toContain('aabbcc');
    expect(text).toContain('1234'); // squawk
    expect(text).toContain('ADS-B'); // source badge
  });

  it('renders subtitle joining type · desc · operator · route', async () => {
    const { container } = renderPage();
    await waitFor(() => {
      if (!container.querySelector('[data-testid="flight-subtitle"]'))
        throw new Error('subtitle not ready');
    });
    const sub = container.querySelector('[data-testid="flight-subtitle"]')!;
    const text = sub.textContent ?? '';
    expect(text).toContain('B789');
    expect(text).toContain('Boeing 787-9');
    expect(text).toContain('LOT Polish Airlines');
    expect(text).toContain('WAW');
    expect(text).toContain('JFK');
  });

  it('renders all 4 metric cells in the grid', async () => {
    const { container } = renderPage();
    await waitFor(() => {
      if (!container.querySelector('[data-testid="flight-metric-alt"]'))
        throw new Error('grid not ready');
    });
    expect(container.querySelector('[data-testid="flight-metric-alt"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="flight-metric-speed"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="flight-metric-dist"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="flight-metric-window"]')).toBeTruthy();
  });

  it('Max alt cell shows the value AND derived vert-rate sublabel', async () => {
    const { container } = renderPage();
    // The numeric MAX value renders unit-aware (41,000 ft OR 12,497 m
    // depending on the units store default in this test runner). Assert the
    // label + the unit-agnostic vert-rate sublabel. The sublabel is derived
    // from the chart query, which resolves after the cell mounts — fold the
    // text into the predicate so waitFor retries until it lands.
    await waitFor(() => {
      const text = container.querySelector('[data-testid="flight-metric-alt"]')?.textContent ?? '';
      if (!text.includes('Max alt') || !text.includes('vert -2400 ft/min'))
        throw new Error('alt cell + vert-rate sublabel not ready');
    });
  });

  it('Max speed cell shows track-at-max sublabel', async () => {
    const { container } = renderPage();
    await waitFor(() => {
      const text = container.querySelector('[data-testid="flight-metric-speed"]')?.textContent ?? '';
      if (!text.includes('track 285°'))
        throw new Error('speed cell + track sublabel not ready');
    });
  });

  it('Max distance cell shows bearing-at-max sublabel (computed client-side)', async () => {
    const { container } = renderPage();
    // Receiver (52, 21) → farthest position (53, 25) is roughly NE.
    await waitFor(() => {
      const text = container.querySelector('[data-testid="flight-metric-dist"]')?.textContent ?? '';
      if (!/bearing \d+°/.test(text))
        throw new Error('dist cell + bearing sublabel not ready');
    });
  });
});

describe('Flight header ACARS badge (2026-06-06)', () => {
  // The badge appears next to the source badge only when VDL2 is available AND
  // the flight has ACARS messages — driven by the same query the ACARS block
  // uses, so badge and block always agree.
  function stubWithVdl2(messages: unknown[]) {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const path = url.split('?')[0];
      let body: unknown = { ok: true };
      if (path.endsWith('/api/settings')) body = { vdl2_enabled: true };
      else if (path.endsWith('/api/health')) body = { vdl2: { available: true } };
      else if (path.includes('/api/vdl2/messages/'))
        body = { messages, next_before_id: null };
      else if (path.endsWith('/api/flights/12345/positions/chart'))
        body = { total: CHART_POSITIONS.length, target: 2000, positions: CHART_POSITIONS };
      else if (path.endsWith('/api/flights/12345/positions'))
        body = { total: CHART_POSITIONS.length, limit: 2000, offset: 0, positions: CHART_POSITIONS };
      else if (path.endsWith('/api/flights/12345/photo')) body = null;
      else if (path.endsWith('/api/flights/12345')) body = FLIGHT_PAYLOAD;
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }) as unknown as typeof fetch;
  }

  it('shows the ACARS badge when the flight has ACARS messages', async () => {
    stubWithVdl2([
      { id: 1, ts: 1_700_001_000, icao_hex: 'aabbcc', label: 'H1',
        body: 'gate report', decoder: 'vdlm2dec' },
    ]);
    const { container } = renderPage();
    await waitFor(() => {
      if (!container.querySelector('[data-testid="flight-acars-badge"]'))
        throw new Error('ACARS badge not present');
    });
    const badge = container.querySelector('[data-testid="flight-acars-badge"]')!;
    expect(badge.textContent).toBe('ACARS');
    // It sits inside the identity row, beside the source badge.
    const identity = container.querySelector('[data-testid="flight-identity"]')!;
    expect(identity.contains(badge)).toBe(true);
  });

  it('omits the ACARS badge when the flight has no ACARS messages', async () => {
    stubWithVdl2([]);
    const { container } = renderPage();
    // Wait for the identity row, then assert the badge is absent.
    await waitFor(() => {
      if (!container.querySelector('[data-testid="flight-identity"]'))
        throw new Error('identity not ready');
    });
    await waitFor(() => {
      expect(container.querySelector('[data-testid="flight-acars-badge"]')).toBeNull();
    });
  });
});
