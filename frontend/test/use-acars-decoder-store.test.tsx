// Shared decoder store: one MessageDecoder for all hook instances, one retry on
// load failure then status 'unavailable', per-message-id decode cache with a
// stable decode callback, airframes-over-local priority.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

// setup.ts globally mocks the hook; this suite needs the real one.
vi.unmock('@/hooks/useAcarsDecoder');

const h = vi.hoisted(() => ({
  ctorCalls: 0,
  failCtor: 0,
  decodeCalls: 0,
  result: null as null | Record<string, unknown>,
}));

vi.mock('@airframes/acars-decoder', () => ({
  MessageDecoder: class {
    constructor() {
      h.ctorCalls++;
      if (h.failCtor > 0) {
        h.failCtor--;
        throw new Error('construct failed');
      }
    }
    decode() {
      h.decodeCalls++;
      return h.result ?? { decoded: false, decoder: { decodeLevel: 'none' }, formatted: { description: 'Not Decoded', items: [] }, raw: {}, remaining: {} };
    }
  },
}));

import { useAcarsDecoder, useAcarsDecoderStatus, __resetForTests } from '@/hooks/useAcarsDecoder';
import type { Vdl2Message } from '@/lib/types';

const msg = (id: number | undefined, label = 'QR', body = 'LIMCEPMO1009'): Vdl2Message =>
  ({ id, ts: 1, label, body }) as Vdl2Message;
const decodedResult = (description: string) => ({
  decoded: true,
  decoder: { name: 'stub', decodeLevel: 'full' },
  formatted: { description, items: [{ type: 't', code: 'C', label: 'Origin', value: 'LIMC' }] },
  raw: {},
  remaining: {},
});

beforeEach(() => {
  __resetForTests();
  h.ctorCalls = 0;
  h.failCtor = 0;
  h.decodeCalls = 0;
  h.result = null;
});
afterEach(() => {
  vi.useRealTimers();
});

describe('shared decoder', () => {
  it('constructs only one MessageDecoder across two hook instances', async () => {
    const a = renderHook(() => useAcarsDecoder());
    const b = renderHook(() => useAcarsDecoder());
    const first = a.result.current;
    await waitFor(() => expect(a.result.current).not.toBe(first));
    await waitFor(() => expect(b.result.current(msg(1))).toBeNull()); // runs through the loaded decoder
    expect(h.ctorCalls).toBe(1);
    // a third, later mount reuses the already-built instance
    renderHook(() => useAcarsDecoder());
    expect(h.ctorCalls).toBe(1);
  });
});

describe('load failure', () => {
  it('retries once, then reports unavailable', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    h.failCtor = 2;
    const status = renderHook(() => useAcarsDecoderStatus());
    renderHook(() => useAcarsDecoder());
    expect(status.result.current).toBe('loading');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(h.ctorCalls).toBe(1);
    expect(status.result.current).toBe('loading'); // first failure: retry pending
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(h.ctorCalls).toBe(2);
    expect(status.result.current).toBe('unavailable');
    // no third attempt, even much later
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(h.ctorCalls).toBe(2);
  });

  it('recovers when the retry succeeds', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    h.failCtor = 1;
    const status = renderHook(() => useAcarsDecoderStatus());
    renderHook(() => useAcarsDecoder());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(h.ctorCalls).toBe(2);
    expect(status.result.current).toBe('ready');
  });

  it('local decoders keep working while unavailable', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    h.failCtor = 2;
    const { result } = renderHook(() => useAcarsDecoder());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(result.current(msg(7, 'SA', '0EV123456V'))?.description).toBe('Media advisory');
  });
});

describe('priority', () => {
  it('airframes wins over the local decoder when both could decode', async () => {
    h.result = decodedResult('Airframes says');
    const { result } = renderHook(() => useAcarsDecoder());
    await waitFor(() => expect(result.current(msg(1, 'SA', '0EV123456V'))?.description).toBe('Airframes says'));
  });

  it('uses the local decoder when airframes returns null', async () => {
    const status = renderHook(() => useAcarsDecoderStatus());
    const { result } = renderHook(() => useAcarsDecoder());
    await waitFor(() => expect(status.result.current).toBe('ready'));
    expect(result.current(msg(1, 'SA', '0EV123456V'))?.description).toBe('Media advisory');
  });
});

describe('decode cache', () => {
  it('keeps a stable decode identity across re-renders and caches by message id', async () => {
    const status = renderHook(() => useAcarsDecoderStatus());
    const { result, rerender } = renderHook(() => useAcarsDecoder());
    await waitFor(() => expect(status.result.current).toBe('ready'));
    const decode = result.current;
    h.decodeCalls = 0;
    decode(msg(1));
    decode(msg(1));
    decode(msg(1));
    expect(h.decodeCalls).toBe(1);
    rerender();
    rerender();
    expect(result.current).toBe(decode);
    result.current(msg(1));
    expect(h.decodeCalls).toBe(1);
    decode(msg(2));
    expect(h.decodeCalls).toBe(2);
  });

  it('caches undecodable (null) results too', async () => {
    const status = renderHook(() => useAcarsDecoderStatus());
    const { result } = renderHook(() => useAcarsDecoder());
    await waitFor(() => expect(status.result.current).toBe('ready'));
    h.decodeCalls = 0;
    expect(result.current(msg(5, 'H1', '#DFBABS'))).toBeNull();
    expect(result.current(msg(5, 'H1', '#DFBABS'))).toBeNull();
    expect(h.decodeCalls).toBe(1);
  });

  it('does not cache messages without an id', async () => {
    const status = renderHook(() => useAcarsDecoderStatus());
    const { result } = renderHook(() => useAcarsDecoder());
    await waitFor(() => expect(status.result.current).toBe('ready'));
    h.decodeCalls = 0;
    result.current(msg(undefined));
    result.current(msg(undefined));
    expect(h.decodeCalls).toBe(2);
  });

  it('clears the cache when the decoder loads (null -> loaded)', async () => {
    h.result = decodedResult('Airframes says');
    const status = renderHook(() => useAcarsDecoderStatus());
    const { result } = renderHook(() => useAcarsDecoder());
    const before = result.current;
    // Before the chunk resolves only local decoders run; this id gets cached as "local/null".
    expect(before(msg(9, 'QR', 'LIMCEPMO1009'))).toBeNull();
    await waitFor(() => expect(status.result.current).toBe('ready'));
    // After load the stale pre-load entry must not shadow the airframes result.
    expect(result.current(msg(9, 'QR', 'LIMCEPMO1009'))?.description).toBe('Airframes says');
  });

  it('bounds the cache size', async () => {
    const status = renderHook(() => useAcarsDecoderStatus());
    const { result } = renderHook(() => useAcarsDecoder());
    await waitFor(() => expect(status.result.current).toBe('ready'));
    for (let i = 1; i <= 5200; i++) result.current(msg(i));
    h.decodeCalls = 0;
    result.current(msg(5200)); // recent: still cached
    expect(h.decodeCalls).toBe(0);
    result.current(msg(1)); // oldest: evicted by the bound
    expect(h.decodeCalls).toBe(1);
  });
});
