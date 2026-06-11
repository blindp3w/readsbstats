// hooks/useVdl2Enabled.ts — the VDL2 gating hooks.
//
// Contracts pinned here:
//  - useVdl2Enabled: true ONLY when settings.vdl2_enabled === true; false
//    while settings are still loading (callers render the no-VDL2 state).
//  - useVdl2Health: the two capability bits are INDEPENDENT (web_conn can
//    work while the cross-DB ATTACH fails).
//  - useVdl2FlightWindow: ±1800 s slack around the flight window (OOOI/gate
//    traffic just before pushback / after landing).
//  - useVdl2FlightMessages: query disabled while unavailable or without an
//    icao (no /api/vdl2 request fires); hasMore iff next_before_id != null.

import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import React from 'react';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/lib/api', () => ({ apiJson: vi.fn() }));

import { apiJson } from '@/lib/api';
import {
  useVdl2Enabled,
  useVdl2Health,
  useVdl2FlightWindow,
  useVdl2FlightMessages,
} from '@/hooks/useVdl2Enabled';

const apiJsonMock = apiJson as Mock;

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

/** Route the mocked apiJson by path prefix. */
function respondWith(routes: Record<string, unknown>) {
  apiJsonMock.mockImplementation((path: string) => {
    for (const [prefix, payload] of Object.entries(routes)) {
      if (path.startsWith(prefix)) return Promise.resolve(payload);
    }
    return Promise.reject(new Error(`unmocked apiJson path: ${path}`));
  });
}

beforeEach(() => {
  apiJsonMock.mockReset();
});

describe('useVdl2Enabled', () => {
  it('is true only when settings.vdl2_enabled === true', async () => {
    respondWith({ settings: { vdl2_enabled: true } });
    const { result } = renderHook(() => useVdl2Enabled(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current).toBe(true));
  });

  it('is false when the flag is off and false while loading', async () => {
    let resolve!: (v: unknown) => void;
    apiJsonMock.mockImplementation(
      () => new Promise((r) => (resolve = r)),
    );
    const { result } = renderHook(() => useVdl2Enabled(), {
      wrapper: makeWrapper(),
    });
    expect(result.current).toBe(false); // still loading → gate closed
    resolve({ vdl2_enabled: false });
    await waitFor(() => expect(apiJsonMock).toHaveBeenCalled());
    expect(result.current).toBe(false);
  });
});

describe('useVdl2Health', () => {
  it('exposes the two capability bits independently', async () => {
    respondWith({
      health: { vdl2: { available: true, attach_available: false } },
    });
    const { result } = renderHook(() => useVdl2Health(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.available).toBe(true);
    expect(result.current.attachAvailable).toBe(false);
  });

  it('reports isLoading before the first health response', () => {
    apiJsonMock.mockImplementation(() => new Promise(() => {}));
    const { result } = renderHook(() => useVdl2Health(), {
      wrapper: makeWrapper(),
    });
    expect(result.current.isLoading).toBe(true);
    expect(result.current.available).toBe(false);
  });
});

describe('useVdl2FlightWindow', () => {
  it('widens the flight window by 1800 s on each side', async () => {
    respondWith({ health: { vdl2: { available: true } } });
    const { result } = renderHook(
      () => useVdl2FlightWindow(10_000, 20_000),
      { wrapper: makeWrapper() },
    );
    expect(result.current.since).toBe(10_000 - 1800);
    expect(result.current.until).toBe(20_000 + 1800);
    await waitFor(() => expect(result.current.available).toBe(true));
  });
});

describe('useVdl2FlightMessages', () => {
  it('never fires the vdl2 query while health says unavailable', async () => {
    respondWith({ health: { vdl2: { available: false } } });
    const { result } = renderHook(
      () => useVdl2FlightMessages('48e95d', 10_000, 20_000),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(apiJsonMock).toHaveBeenCalled());
    expect(result.current.available).toBe(false);
    expect(result.current.messages).toEqual([]);
    const vdl2Calls = apiJsonMock.mock.calls.filter(([p]) =>
      String(p).startsWith('vdl2/'),
    );
    expect(vdl2Calls).toEqual([]);
  });

  it('never fires the vdl2 query without an icao', async () => {
    respondWith({ health: { vdl2: { available: true } } });
    renderHook(() => useVdl2FlightMessages('', 10_000, 20_000), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(apiJsonMock).toHaveBeenCalled());
    const vdl2Calls = apiJsonMock.mock.calls.filter(([p]) =>
      String(p).startsWith('vdl2/'),
    );
    expect(vdl2Calls).toEqual([]);
  });

  it('hasMore is true iff next_before_id is present', async () => {
    respondWith({
      health: { vdl2: { available: true } },
      'vdl2/messages/48e95d': {
        messages: [{ id: 7, ts: 1, body: 'hi' }],
        next_before_id: 7,
      },
    });
    const { result } = renderHook(
      () => useVdl2FlightMessages('48e95d', 10_000, 20_000),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.hasMore).toBe(true);
  });

  it('hasMore is false on the last page', async () => {
    respondWith({
      health: { vdl2: { available: true } },
      'vdl2/messages/48e95d': {
        messages: [{ id: 7, ts: 1, body: 'hi' }],
        next_before_id: null,
      },
    });
    const { result } = renderHook(
      () => useVdl2FlightMessages('48e95d', 10_000, 20_000),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.hasMore).toBe(false);
  });
});
