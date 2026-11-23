// Flight page wiring for ACARS position fixes: RouteMap gets `acarsFixes` (leg-filtered),
// the winds-aloft card appears only with atmospherics, and with VDL2 off/unavailable no
// /api/vdl2/positions request is made at all.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';

let routeMapProps: Record<string, any> | null = null;
vi.mock('@/components/RouteMap', () => ({
  default: (props: Record<string, any>) => {
    routeMapProps = props;
    return <div data-testid="route-map-stub" />;
  },
}));

const FIRST = 1_749_060_000;
const LAST = 1_749_070_000;

const detail = {
  flight: {
    id: 5,
    icao_hex: '48e95d',
    callsign: 'LO1',
    registration: 'SP-LYF',
    aircraft_type: 'B38M',
    type_desc: null,
    flags: 0,
    squawk: null,
    primary_source: 'adsb',
    first_seen: FIRST,
    last_seen: LAST,
    duration_sec: LAST - FIRST,
    max_alt_baro: 35000,
    max_gs: 450,
    max_distance_nm: 100,
    total_positions: 10,
    origin_icao: null,
    dest_icao: null,
  },
  receiver_lat: 52,
  receiver_lon: 21,
  other_flights: [],
};

const fix = (ts: number, extra = {}) => ({
  lat: 52,
  lon: 21,
  icao_hex: '48e95d',
  ts,
  label: 'H1',
  precise: true,
  source: 'h1-abs',
  ...extra,
});

let urls: string[] = [];
function stubFetch(o: { enabled: boolean; available: boolean; points?: unknown[] }) {
  urls = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : (input as Request).url ?? String(input);
    urls.push(url);
    let body: unknown = {};
    if (url.includes('/api/settings')) body = { vdl2_enabled: o.enabled };
    else if (url.includes('/api/health')) body = { vdl2: { available: o.available } };
    else if (url.includes('/api/vdl2/positions/'))
      body = { points: o.points ?? [], count: (o.points ?? []).length };
    else if (url.includes('/api/vdl2/messages')) body = { messages: [], next_before_id: null };
    else if (/\/api\/flights\/5\/positions/.test(url)) body = { total: 0, positions: [] };
    else if (/\/api\/flights\/5\/photo/.test(url)) body = null;
    else if (/\/api\/flights\/5$/.test(url)) body = detail;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
}

async function renderFlight() {
  const { default: FlightPage } = await import('@/pages/Flight');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={['/flight/5']}>
          <Routes>
            <Route path="/flight/:id" element={<FlightPage />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  routeMapProps = null;
  globalThis.localStorage.clear();
});

describe('Flight page ACARS fixes', () => {
  it('passes only in-flight fixes to RouteMap and shows the winds card', async () => {
    stubFetch({
      enabled: true,
      available: true,
      points: [
        fix(FIRST - 300, { temp_c: -10 }), // previous leg: must not leak
        fix(FIRST + 100, { alt: 20000, temp_c: -30, wind_dir: 270, wind_kt: 40 }),
        fix(FIRST + 200),
        fix(LAST + 300),
      ],
    });
    await renderFlight();
    await waitFor(() => expect(routeMapProps?.acarsFixes).toHaveLength(2));
    expect(routeMapProps!.acarsFixes.map((f: any) => f.ts)).toEqual([FIRST + 100, FIRST + 200]);
    expect(screen.getAllByTestId('winds-aloft-row')).toHaveLength(1);
    const req = urls.find((u) => u.includes('/api/vdl2/positions/48e95d'))!;
    expect(req).toContain(`since=${FIRST - 600}`);
    expect(req).toContain(`until=${LAST + 600}`);
  });

  it('omits the prop and the winds card when no fix has atmospherics / none exist', async () => {
    stubFetch({ enabled: true, available: true, points: [] });
    await renderFlight();
    await waitFor(() => expect(urls.some((u) => u.includes('/api/vdl2/positions/'))).toBe(true));
    await waitFor(() => expect(routeMapProps).not.toBeNull());
    expect(routeMapProps!.acarsFixes).toBeUndefined();
    expect(screen.queryByTestId('flight-winds-card')).toBeNull();
  });

  it.each([
    ['disabled', { enabled: false, available: true }],
    ['unavailable', { enabled: true, available: false }],
  ])('makes no positions request and shows nothing when VDL2 is %s', async (_n, o) => {
    stubFetch({ ...o, points: [fix(FIRST + 100, { temp_c: -30 })] });
    await renderFlight();
    await waitFor(() => expect(routeMapProps).not.toBeNull());
    await waitFor(() => expect(urls.some((u) => u.includes('/api/health'))).toBe(true));
    await new Promise((r) => setTimeout(r, 50));
    expect(urls.some((u) => u.includes('/api/vdl2/positions'))).toBe(false);
    expect(routeMapProps!.acarsFixes).toBeUndefined();
    expect(screen.queryByTestId('flight-winds-card')).toBeNull();
  });
});
