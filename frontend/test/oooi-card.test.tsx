/**
 * Flight-detail OOOI card (Phase D, experimental). Renders parsed Out/Off/On/In
 * block times and a ✓/✗ route-confirmation chip vs the scheduled origin/dest.
 * Renders NOTHING when no OOOI body parsed and there's no dsta/route fallback.
 * `route` (CM logon / label-49 movement report) is the fallback when no OOOI.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { OooiCard } from '@/components/vdl2/OooiCard';

interface Stub {
  vdl2_enabled?: boolean;
  oooi?: unknown;
  oooiFails?: boolean;
}

function stubFetch(s: Stub) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    let body: unknown = {};
    if (url.includes('/api/settings')) body = { vdl2_enabled: s.vdl2_enabled ?? false };
    else if (url.includes('/api/health'))
      body = { vdl2: { enabled: s.vdl2_enabled ?? false, available: s.vdl2_enabled ?? false } };
    else if (url.includes('/api/vdl2/oooi') && s.oooiFails) return new Response('boom', { status: 500 });
    else if (url.includes('/api/vdl2/oooi')) body = s.oooi ?? { dep: null, arr: null, dsta: null, has_oooi: false };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
}

function wrap(props: Partial<Parameters<typeof OooiCard>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <OooiCard
        icao="48e95d"
        firstSeen={1000}
        lastSeen={2000}
        scheduledOrigin="EPWA"
        scheduledDest="EGLL"
        {...props}
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  globalThis.localStorage.clear();
});

describe('OooiCard', () => {
  it('renders block times and a route-match chip when the route agrees', async () => {
    stubFetch({
      vdl2_enabled: true,
      oooi: {
        has_oooi: true,
        dsta: null,
        dep: { type: 'DEP', dep_icao: 'EPWA', dest_icao: 'EGLL', t_out: '0030', t_off: '0042' },
        arr: { type: 'ARR', dep_icao: 'EPWA', dest_icao: 'EGLL', t_on: '0210', t_in: '0218' },
      },
    });
    wrap();
    await waitFor(() => screen.getByTestId('flight-oooi-card'));
    expect(screen.getByText('00:30 UTC')).toBeTruthy(); // t_out formatted
    expect(screen.getByText('02:18 UTC')).toBeTruthy(); // t_in formatted
    expect(screen.getAllByTestId('oooi-route-match').length).toBe(2);
  });

  it('flags a route mismatch against the scheduled destination', async () => {
    stubFetch({
      vdl2_enabled: true,
      oooi: {
        has_oooi: true,
        dsta: null,
        dep: { type: 'DEP', dep_icao: 'EPWA', dest_icao: 'EDDF', t_out: '0030' },
        arr: null,
      },
    });
    wrap({ scheduledDest: 'EGLL' });
    await waitFor(() => screen.getByTestId('flight-oooi-card'));
    expect(screen.getByTestId('oooi-route-mismatch')).toBeTruthy();
  });

  it('shows the dsta-only note when no OOOI parsed but a destination is known', async () => {
    stubFetch({
      vdl2_enabled: true,
      oooi: { has_oooi: false, dsta: 'EPWA', dep: null, arr: null },
    });
    wrap({ scheduledOrigin: null, scheduledDest: null });
    await waitFor(() => screen.getByTestId('flight-oooi-dsta-only'));
  });

  it('renders a CM-logon route with its note when no OOOI parsed', async () => {
    stubFetch({
      vdl2_enabled: true,
      oooi: {
        has_oooi: false,
        dsta: null,
        dep: null,
        arr: null,
        route: { dep_icao: 'EPWA', dest_icao: 'EGLL', flight: 'RYR8414', source: 'cm' },
      },
    });
    wrap();
    await waitFor(() => screen.getByTestId('flight-oooi-card'));
    expect(screen.getByText('EPWA')).toBeTruthy();
    expect(screen.getByText('EGLL')).toBeTruthy();
    expect(screen.getByTestId('flight-oooi-route-only').textContent).toContain('ATN logon');
    expect(screen.queryByTestId('flight-oooi-dsta-only')).toBeNull();
    expect(screen.getAllByTestId('oooi-route-match').length).toBe(2);
  });

  it('flags a route-only mismatch against the scheduled route', async () => {
    stubFetch({
      vdl2_enabled: true,
      oooi: {
        has_oooi: false,
        dsta: null,
        dep: null,
        arr: null,
        route: { dep_icao: 'EPWA', dest_icao: 'EDDF', flight: null, source: 'cm' },
      },
    });
    wrap({ scheduledDest: 'EGLL' });
    await waitFor(() => screen.getByTestId('flight-oooi-card'));
    expect(screen.getAllByTestId('oooi-route-match').length).toBe(1);
    expect(screen.getByTestId('oooi-route-mismatch')).toBeTruthy();
  });

  it('shows the airline-report note for a label-49 route', async () => {
    stubFetch({
      vdl2_enabled: true,
      oooi: {
        has_oooi: false,
        dsta: 'EGLL',
        dep: null,
        arr: null,
        route: { dep_icao: 'EPWA', dest_icao: 'EGLL', flight: null, source: 'label49' },
      },
    });
    wrap();
    await waitFor(() => screen.getByTestId('flight-oooi-route-only'));
    expect(screen.getByTestId('flight-oooi-route-only').textContent).toContain(
      'airline movement report',
    );
    expect(screen.queryByTestId('flight-oooi-dsta-only')).toBeNull();
  });

  it.each([
    ['label26', 'an Air France ETA report'],
    ['label20', 'a KLM route report'],
  ])('shows the %s note for a route-only summary', async (source, phrase) => {
    stubFetch({
      vdl2_enabled: true,
      oooi: {
        has_oooi: false,
        dsta: null,
        dep: null,
        arr: null,
        route: { dep_icao: 'EPWA', dest_icao: 'EGLL', flight: null, source },
      },
    });
    wrap();
    await waitFor(() => screen.getByTestId('flight-oooi-route-only'));
    expect(screen.getByTestId('flight-oooi-route-only').textContent).toContain(
      `No OOOI block times parsed \u2014 route from ${phrase}.`,
    );
  });

  it('renders when the summary carries nothing but a route', async () => {
    stubFetch({
      vdl2_enabled: true,
      oooi: { has_oooi: false, route: { dep_icao: null, dest_icao: 'EGLL', flight: null, source: 'cm' } },
    });
    wrap({ scheduledOrigin: null, scheduledDest: null });
    await waitFor(() => screen.getByTestId('flight-oooi-card'));
    expect(screen.getByText('EGLL')).toBeTruthy();
  });

  it('prefers OOOI event values over the route fallback', async () => {
    stubFetch({
      vdl2_enabled: true,
      oooi: {
        has_oooi: true,
        dsta: null,
        dep: { type: 'DEP', dep_icao: 'EPWA', dest_icao: 'EGLL', t_out: '0030' },
        arr: null,
        route: { dep_icao: 'EPKK', dest_icao: 'EDDF', flight: null, source: 'cm' },
      },
    });
    wrap();
    await waitFor(() => screen.getByTestId('flight-oooi-card'));
    expect(screen.getByText('EPWA')).toBeTruthy();
    expect(screen.queryByText('EPKK')).toBeNull();
    expect(screen.queryByText('EDDF')).toBeNull();
    expect(screen.queryByTestId('flight-oooi-route-only')).toBeNull();
  });

  it('renders nothing when there is no OOOI, no dsta and no route', async () => {
    stubFetch({ vdl2_enabled: true, oooi: { has_oooi: false, dsta: null, dep: null, arr: null } });
    const { container } = wrap();
    await waitFor(() => {
      expect(container.querySelector('[data-testid="flight-oooi-card"]')).toBeNull();
    });
  });

  it('renders nothing when the feature is disabled', async () => {
    stubFetch({ vdl2_enabled: false });
    const { container } = wrap();
    await waitFor(() => {
      expect(container.querySelector('[data-testid="flight-oooi-card"]')).toBeNull();
    });
  });

  it('renders a muted error line (not null) when the OOOI query fails', async () => {
    stubFetch({ vdl2_enabled: true, oooiFails: true });
    wrap();
    const el = await screen.findByTestId('flight-oooi-error');
    expect(el.textContent).toMatch(/Failed to load/i);
    expect(screen.queryByTestId('flight-oooi-card')).toBeNull();
  });

  it('stays silent on a query error when VDL2 is unavailable', async () => {
    stubFetch({ vdl2_enabled: false, oooiFails: true });
    const { container } = wrap();
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    expect(container.querySelector('[data-testid="flight-oooi-error"]')).toBeNull();
  });
});

describe('OooiCard block-time timeline', () => {
  const oooi = (dep: object | null, arr: object | null) => ({
    has_oooi: true,
    dsta: null,
    dep: dep && { type: 'DEP', dep_icao: 'EPWA', dest_icao: 'EGLL', ...dep },
    arr: arr && { type: 'ARR', dep_icao: 'EPWA', dest_icao: 'EGLL', ...arr },
  });

  it('shows four points with taxi/airborne durations and a text alternative', async () => {
    stubFetch({
      vdl2_enabled: true,
      oooi: oooi({ t_out: '0030', t_off: '0042' }, { t_on: '0210', t_in: '0218' }),
    });
    wrap();
    await waitFor(() => screen.getByTestId('oooi-timeline'));
    for (const k of ['out', 'off', 'on', 'in']) expect(screen.getByTestId(`oooi-point-${k}`)).toBeTruthy();
    const legs = screen.getAllByTestId('oooi-leg').map((l) => l.textContent);
    expect(legs).toEqual(['Taxi out12 min', 'Airborne1 h 28 min', 'Taxi in8 min']);
    expect(screen.getByTestId('oooi-timeline-summary').textContent).toBe(
      'OUT 00:30 UTC, Taxi out 12 min, OFF 00:42 UTC, Airborne 1 h 28 min, ON 02:10 UTC, Taxi in 8 min, IN 02:18 UTC',
    );
  });

  it('renders only known points; a leg across a gap has no duration', async () => {
    stubFetch({
      vdl2_enabled: true,
      oooi: oooi({ t_out: '0030' }, { t_on: '0210', t_in: '0218' }),
    });
    wrap();
    await waitFor(() => screen.getByTestId('oooi-timeline'));
    expect(screen.queryByTestId('oooi-point-off')).toBeNull();
    const legs = screen.getAllByTestId('oooi-leg').map((l) => l.textContent);
    expect(legs).toEqual(['', 'Taxi in8 min']); // OUT->ON is not a named leg
  });

  it('skips durations when times are out of order', async () => {
    stubFetch({
      vdl2_enabled: true,
      oooi: oooi({ t_out: '0100', t_off: '0050' }, { t_on: '0210', t_in: '0200' }),
    });
    wrap();
    await waitFor(() => screen.getByTestId('oooi-timeline'));
    const summary = screen.getByTestId('oooi-timeline-summary').textContent!;
    expect(summary).not.toMatch(/Taxi out|Taxi in/);
    expect(summary).not.toMatch(/-\d/);
    expect(summary).toContain('Airborne 1 h 20 min');
  });

  it('lets only the airborne leg cross midnight UTC', async () => {
    stubFetch({
      vdl2_enabled: true,
      oooi: oooi({ t_out: '2340', t_off: '2350' }, { t_on: '0130', t_in: '0140' }),
    });
    wrap();
    await waitFor(() => screen.getByTestId('oooi-timeline'));
    expect(screen.getByTestId('oooi-timeline-summary').textContent).toContain('Airborne 1 h 40 min');
  });

  it('falls back to plain rows with a single known time', async () => {
    stubFetch({ vdl2_enabled: true, oooi: oooi({ t_out: '0030' }, null) });
    wrap();
    await waitFor(() => screen.getByTestId('flight-oooi-card'));
    expect(screen.queryByTestId('oooi-timeline')).toBeNull();
    expect(screen.getByText('00:30 UTC')).toBeTruthy();
  });

  it.each([
    ['r64', 'Air France engine report'],
    ['abs', 'ACMS position report'],
    ['b44', 'take-off report'],
    ['fst01', 'label 15 flight status'],
  ])('notes the %s route source', async (source, text) => {
    stubFetch({
      vdl2_enabled: true,
      oooi: {
        has_oooi: false,
        dsta: null,
        dep: null,
        arr: null,
        route: { dep_icao: 'EPWA', dest_icao: 'EGLL', flight: null, source },
      },
    });
    wrap();
    await waitFor(() => screen.getByTestId('flight-oooi-route-only'));
    expect(screen.getByTestId('flight-oooi-route-only').textContent).toContain(text);
  });
});
