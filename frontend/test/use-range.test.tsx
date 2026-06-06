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

  // BUG-14: an inverted (from >= to) custom window from the URL must fall back
  // to the default preset rather than being honoured as a zero/negative-length
  // window (mirrors CustomRangeForm.apply's guard).
  it('falls back to the default preset when from >= to (inverted)', () => {
    const { result } = renderHook(() => useRange('7d'), {
      wrapper: wrap('/?from=1700000456&to=1700000123'),
    });
    const { value, from, to } = result.current.state;
    expect(value).toBe('7d');
    // Resolved window is the 7d preset, not the inverted custom one.
    expect(to! - from!).toBe(7 * 86400);
    expect(to! % 300).toBe(0);
  });

  it('falls back to the default preset when from === to (zero-length)', () => {
    const { result } = renderHook(() => useRange('7d'), {
      wrapper: wrap('/?from=1700000123&to=1700000123'),
    });
    expect(result.current.state.value).toBe('7d');
    expect(result.current.state.to! - result.current.state.from!).toBe(7 * 86400);
  });

  // code-review: an empty/blank `from` (a form that cleared the field, or a
  // hand-edited URL) must not coerce via Number('')===0 into a hidden 1970
  // window — it falls back to the default preset.
  it('falls back to the default preset when from is empty (Number("")===0 trap)', () => {
    const { result } = renderHook(() => useRange('7d'), {
      wrapper: wrap('/?from=&to=1700000456'),
    });
    expect(result.current.state.value).toBe('7d');
    expect(result.current.state.to! - result.current.state.from!).toBe(7 * 86400);
  });
});
