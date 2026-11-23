// useVdl2FlightPositions — gating (no request unless VDL2 is enabled AND the db is
// queryable), the clamped since/until the request carries, and leg filtering.
import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import React from 'react';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/lib/api', () => ({ apiJson: vi.fn() }));

import { apiJson } from '@/lib/api';
import { useVdl2FlightPositions } from '@/hooks/useVdl2Enabled';

const apiJsonMock = apiJson as Mock;

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function setup(opts: { enabled: boolean; available: boolean; points?: unknown[] }) {
  apiJsonMock.mockImplementation((path: string) => {
    if (path.startsWith('settings')) return Promise.resolve({ vdl2_enabled: opts.enabled });
    if (path.startsWith('health')) return Promise.resolve({ vdl2: { available: opts.available } });
    if (path.startsWith('vdl2/positions/'))
      return Promise.resolve({ points: opts.points ?? [], count: (opts.points ?? []).length });
    return Promise.reject(new Error(`unmocked ${path}`));
  });
}

const fixCalls = () =>
  apiJsonMock.mock.calls.map((c) => c[0] as string).filter((p) => p.startsWith('vdl2/positions'));

const p = (ts: number | null, extra = {}) => ({
  lat: 52,
  lon: 21,
  icao_hex: '4ca123',
  ts,
  label: null,
  precise: true,
  ...extra,
});

beforeEach(() => {
  apiJsonMock.mockReset();
});

describe('useVdl2FlightPositions gating', () => {
  it.each([
    ['VDL2 disabled', { enabled: false, available: true }],
    ['vdl2.db unavailable', { enabled: true, available: false }],
  ])('makes no positions request when %s', async (_n, o) => {
    setup(o);
    const { result } = renderHook(() => useVdl2FlightPositions('4ca123', 1000, 2000), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(apiJsonMock).toHaveBeenCalledWith('health'));
    await new Promise((r) => setTimeout(r, 30));
    expect(fixCalls()).toEqual([]);
    expect(result.current.available).toBe(false);
    expect(result.current.fixes).toEqual([]);
  });

  it('makes no request without an icao', async () => {
    setup({ enabled: true, available: true });
    renderHook(() => useVdl2FlightPositions('', 1000, 2000), { wrapper: wrapper() });
    await new Promise((r) => setTimeout(r, 30));
    expect(fixCalls()).toEqual([]);
  });
});

describe('useVdl2FlightPositions fetching', () => {
  it('requests ±600 s around the flight and drops fixes outside it', async () => {
    setup({
      enabled: true,
      available: true,
      points: [p(900), p(1000), p(1500, { alt: 30000 }), p(2000), p(2400), p(null)],
    });
    const { result } = renderHook(() => useVdl2FlightPositions('4ca123', 1000, 2000), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.fixes.length).toBe(3));
    expect(result.current.fixes.map((f) => f.ts)).toEqual([1000, 1500, 2000]);
    expect(result.current.available).toBe(true);
    expect(fixCalls()).toEqual(['vdl2/positions/4ca123?since=400&until=2600']);
  });

  it('clamps a >48 h flight to the last 48 h', async () => {
    const last = 1_000_000;
    setup({ enabled: true, available: true });
    renderHook(() => useVdl2FlightPositions('4ca123', last - 100 * 3600, last), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(fixCalls().length).toBe(1));
    const q = new URLSearchParams(fixCalls()[0].split('?')[1]);
    expect(Number(q.get('until'))).toBe(last + 600);
    expect(Number(q.get('until')) - Number(q.get('since'))).toBe(48 * 3600);
  });
});
