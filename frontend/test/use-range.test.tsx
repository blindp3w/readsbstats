import { describe, it, expect } from 'vitest';
import { renderHook } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { FC, ReactNode } from 'react';

import { useRange } from '@/components/useRange';

// The Stats page defaults to 7d (fast filtered path). presetWindow() quantizes
// the window end to 5-min buckets so the resolved from/to — and thus the
// /api/stats cache key — stay stable across reloads; otherwise `to = now`
// changes every second and every load misses the backend response cache.

function wrap(initial = '/'): FC<{ children: ReactNode }> {
  return ({ children }) => (
    <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>
  );
}

describe('useRange', () => {
  it('resolves the default preset to a 5-min-quantized window on first render', () => {
    const { result } = renderHook(() => useRange('7d'), { wrapper: wrap('/') });
    const { value, from, to } = result.current.state;
    expect(value).toBe('7d');
    expect(from).toBeDefined();
    expect(to).toBeDefined();
    expect(to! % 300).toBe(0); // bucket-aligned end
    expect(from! % 300).toBe(0);
    expect(to! - from!).toBe(7 * 86400);
  });

  it('all-time resolves to no window', () => {
    const { result } = renderHook(() => useRange('all'), { wrapper: wrap('/') });
    expect(result.current.state.value).toBe('all');
    expect(result.current.state.from).toBeUndefined();
    expect(result.current.state.to).toBeUndefined();
  });

  it('honours an explicit preset from the URL, still quantized', () => {
    const { result } = renderHook(() => useRange('7d'), {
      wrapper: wrap('/?range=24h'),
    });
    const { value, from, to } = result.current.state;
    expect(value).toBe('24h');
    expect(to! % 300).toBe(0);
    expect(to! - from!).toBe(86400);
  });

  it('uses custom from/to verbatim (not quantized)', () => {
    const { result } = renderHook(() => useRange('7d'), {
      wrapper: wrap('/?from=1700000123&to=1700000456'),
    });
    const { value, from, to } = result.current.state;
    expect(value).toBe('custom');
    expect(from).toBe(1700000123);
    expect(to).toBe(1700000456);
  });
});
