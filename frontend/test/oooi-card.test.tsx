/**
 * Flight-detail OOOI card (Phase D, experimental). Renders parsed Out/Off/On/In
 * block times and a ✓/✗ route-confirmation chip vs the scheduled origin/dest.
 * Renders NOTHING when no OOOI body parsed and there's no dsta fallback.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { OooiCard } from '@/components/vdl2/OooiCard';

interface Stub {
  vdl2_enabled?: boolean;
  oooi?: unknown;
}

function stubFetch(s: Stub) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    let body: unknown = {};
    if (url.includes('/api/settings')) body = { vdl2_enabled: s.vdl2_enabled ?? false };
    else if (url.includes('/api/health'))
      body = { vdl2: { enabled: s.vdl2_enabled ?? false, available: s.vdl2_enabled ?? false } };
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
    expect(screen.getByText('00:30')).toBeTruthy(); // t_out formatted
    expect(screen.getByText('02:18')).toBeTruthy(); // t_in formatted
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

  it('renders nothing when there is no OOOI and no dsta', async () => {
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
});
