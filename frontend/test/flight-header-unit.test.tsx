/**
 * Direct-render unit test for the extracted FlightHeader component
 * (src/components/flight/FlightHeader.tsx). The full-page smoke coverage
 * lives in flight-header.test.tsx; this asserts the component renders
 * standalone with a minimal fixture so the extraction is independently
 * exercised.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { FlightHeader } from '@/components/flight/FlightHeader';
import type { FlightDetail, Position } from '@/components/flight/types';

const DETAIL: FlightDetail = {
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

const POSITIONS: Position[] = [
  {
    ts: 1_700_001_800,
    lat: 52.5,
    lon: 22.0,
    alt_baro: 41000,
    alt_geom: null,
    gs: 521,
    track: 285,
    baro_rate: -2400,
    rssi: -8,
    source_type: 'adsb_icao',
  },
];

beforeEach(() => {
  // VDL2 gating queries (settings / health) resolve to "off" so the ACARS
  // badge stays absent — the component still renders the rest of the header.
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  ) as unknown as typeof fetch;
  globalThis.localStorage.clear();
});

function renderHeader() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter>
          <FlightHeader
            detail={DETAIL}
            photoQ={{ data: null, isLoading: false }}
            positions={POSITIONS}
            receiverLat={DETAIL.receiver_lat}
            receiverLon={DETAIL.receiver_lon}
          />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe('FlightHeader (direct render)', () => {
  it('renders the header card with identity, subtitle and metric cells', async () => {
    const { container } = renderHeader();
    await waitFor(() => {
      if (!container.querySelector('[data-testid="flight-header-card"]'))
        throw new Error('header not ready');
    });
    const identity = container.querySelector('[data-testid="flight-identity"]')!;
    expect(identity.textContent).toContain('SP-LRF');
    expect(identity.textContent).toContain('LO281');
    expect(container.querySelector('[data-testid="flight-metric-alt"]')).toBeTruthy();
    // At-max sublabel is derived from POSITIONS (single pass, client-side).
    const altCell = container.querySelector('[data-testid="flight-metric-alt"]');
    expect(altCell?.textContent).toContain('vert -2400 ft/min');
  });
});
