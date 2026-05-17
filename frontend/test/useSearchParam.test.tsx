import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import type { FC, ReactNode } from 'react';

import { useSearchParam, useSearchParamBatch } from '@/hooks/useSearchParam';

// Audit-12 #200 — `useSearchParamBatch` is the documented workaround for
// React Router v7's stale-state bug when `setSearchParams` is called twice
// in the same event handler. CLAUDE.md flags it as the highest-leverage
// untested primitive (regression here corrupts every multi-param URL
// update in the SPA).

function wrap(initial = '/'): FC<{ children: ReactNode }> {
  return ({ children }) => (
    <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>
  );
}

describe('useSearchParam', () => {
  it('returns the default when the param is absent', () => {
    const { result } = renderHook(() => useSearchParam('q', ''), {
      wrapper: wrap('/'),
    });
    expect(result.current[0]).toBe('');
  });

  it('returns the URL value when present', () => {
    const { result } = renderHook(() => useSearchParam('q', ''), {
      wrapper: wrap('/?q=hello'),
    });
    expect(result.current[0]).toBe('hello');
  });

  it('coerces numeric default to a number', () => {
    const { result } = renderHook(() => useSearchParam('page', 0), {
      wrapper: wrap('/?page=3'),
    });
    expect(result.current[0]).toBe(3);
  });

  it('falls back to numeric default when value is not finite', () => {
    const { result } = renderHook(() => useSearchParam('page', 0), {
      wrapper: wrap('/?page=NaN'),
    });
    expect(result.current[0]).toBe(0);
  });

  it('setting to default strips the param from the URL', () => {
    function Probe() {
      const [page, setPage] = useSearchParam('page', 0);
      const loc = useLocation();
      return { page, setPage, search: loc.search };
    }
    const { result } = renderHook(Probe, { wrapper: wrap('/?page=5') });
    expect(result.current.page).toBe(5);
    act(() => result.current.setPage(0));
    expect(result.current.search).toBe('');
    expect(result.current.page).toBe(0);
  });

  it('setting to empty-string strips the param for string-typed callers', () => {
    function Probe() {
      const [q, setQ] = useSearchParam('q', '');
      const loc = useLocation();
      return { q, setQ, search: loc.search };
    }
    const { result } = renderHook(Probe, { wrapper: wrap('/?q=hello') });
    act(() => result.current.setQ(''));
    expect(result.current.search).toBe('');
  });
});

describe('useSearchParamBatch — v7 stale-state workaround', () => {
  it('commits two param changes in one call', () => {
    function Probe() {
      const update = useSearchParamBatch();
      const loc = useLocation();
      return { update, search: loc.search };
    }
    const { result } = renderHook(Probe, { wrapper: wrap('/') });
    act(() => result.current.update({ flags: 'military', offset: 50 }));
    const params = new URLSearchParams(result.current.search);
    expect(params.get('flags')).toBe('military');
    expect(params.get('offset')).toBe('50');
  });

  it('subsequent batched updates merge with the existing URL', () => {
    function Probe() {
      const update = useSearchParamBatch();
      const loc = useLocation();
      return { update, search: loc.search };
    }
    const { result } = renderHook(Probe, {
      wrapper: wrap('/?range=24h&offset=100'),
    });
    // Add a new param + change an existing one in a single batch
    act(() => result.current.update({ flags: 'interesting', offset: 0 }));
    const params = new URLSearchParams(result.current.search);
    // offset=0 is treated as default → removed
    expect(params.get('offset')).toBeNull();
    // existing range untouched
    expect(params.get('range')).toBe('24h');
    // new flag set
    expect(params.get('flags')).toBe('interesting');
  });

  it('explicit null removes the param', () => {
    function Probe() {
      const update = useSearchParamBatch();
      const loc = useLocation();
      return { update, search: loc.search };
    }
    const { result } = renderHook(Probe, { wrapper: wrap('/?q=hello&p=1') });
    act(() => result.current.update({ q: null }));
    const params = new URLSearchParams(result.current.search);
    expect(params.get('q')).toBeNull();
    // unrelated param untouched
    expect(params.get('p')).toBe('1');
  });

  it('zero numeric value is treated as default (removed)', () => {
    function Probe() {
      const update = useSearchParamBatch();
      const loc = useLocation();
      return { update, search: loc.search };
    }
    const { result } = renderHook(Probe, { wrapper: wrap('/?offset=100') });
    act(() => result.current.update({ offset: 0 }));
    const params = new URLSearchParams(result.current.search);
    expect(params.get('offset')).toBeNull();
  });

  it('empty-string value is treated as default (removed)', () => {
    function Probe() {
      const update = useSearchParamBatch();
      const loc = useLocation();
      return { update, search: loc.search };
    }
    const { result } = renderHook(Probe, { wrapper: wrap('/?q=hello') });
    act(() => result.current.update({ q: '' }));
    const params = new URLSearchParams(result.current.search);
    expect(params.get('q')).toBeNull();
  });

  it('single call composes multiple updates atomically (this IS the fix)', () => {
    // The v7 stale-state bug appears when callers issue two
    // `setSearchParams` calls back-to-back in the same event handler — the
    // second `prev` is stale and overwrites the first update. The fix is
    // NOT to make the batch helper magically handle two-call usage; it's
    // to require callers to pass all changes to one call. This test pins
    // the contract: one call carrying multiple keys updates atomically.
    function Probe() {
      const update = useSearchParamBatch();
      const loc = useLocation();
      return { update, search: loc.search };
    }
    const { result } = renderHook(Probe, { wrapper: wrap('/?range=24h') });
    act(() => result.current.update({ flags: 'military', offset: 50, page: 0 }));
    const params = new URLSearchParams(result.current.search);
    // page=0 is default-stripped; flags + offset committed; range preserved.
    expect(params.get('flags')).toBe('military');
    expect(params.get('offset')).toBe('50');
    expect(params.get('page')).toBeNull();
    expect(params.get('range')).toBe('24h');
  });
});
