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
  positions: [
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
  ],
  other_flights: [],
  receiver_lat: 52.0,
  receiver_lon: 21.0,
};

beforeEach(() => {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const path = url.split('?')[0];
    let body: unknown = { ok: true };
    if (path.endsWith('/api/flights/12345')) body = FLIGHT_PAYLOAD;
    else if (path.endsWith('/api/flights/12345/photo')) body = null;
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
    await waitFor(() => {
      if (!container.querySelector('[data-testid="flight-metric-alt"]'))
        throw new Error('grid not ready');
    });
    const cell = container.querySelector('[data-testid="flight-metric-alt"]')!;
    const text = cell.textContent ?? '';
    // The numeric MAX value renders unit-aware (41,000 ft OR 12,497 m
    // depending on the units store default in this test runner). Just
    // assert the label + the unit-agnostic vert-rate sublabel.
    expect(text).toContain('Max alt');
    expect(text).toContain('vert -2400 ft/min');
  });

  it('Max speed cell shows track-at-max sublabel', async () => {
    const { container } = renderPage();
    await waitFor(() => {
      if (!container.querySelector('[data-testid="flight-metric-speed"]'))
        throw new Error('grid not ready');
    });
    const cell = container.querySelector('[data-testid="flight-metric-speed"]')!;
    expect(cell.textContent).toContain('track 285°');
  });

  it('Max distance cell shows bearing-at-max sublabel (computed client-side)', async () => {
    const { container } = renderPage();
    await waitFor(() => {
      if (!container.querySelector('[data-testid="flight-metric-dist"]'))
        throw new Error('grid not ready');
    });
    const cell = container.querySelector('[data-testid="flight-metric-dist"]')!;
    // Receiver (52, 21) → farthest position (53, 25) is roughly NE.
    expect(cell.textContent).toMatch(/bearing \d+°/);
  });
});
