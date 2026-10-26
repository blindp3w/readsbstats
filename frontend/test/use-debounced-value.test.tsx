// hooks/useDebouncedValue.ts — trailing debounce of a changing value.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useDebouncedValue } from '@/hooks/useDebouncedValue';

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe('useDebouncedValue', () => {
  it('returns the initial value immediately', () => {
    const { result } = renderHook(() => useDebouncedValue('a', 300));
    expect(result.current).toBe('a');
  });

  it('only follows a change after the delay has elapsed', () => {
    const { result, rerender } = renderHook(({ v }) => useDebouncedValue(v, 300), {
      initialProps: { v: 'a' },
    });
    rerender({ v: 'b' });
    act(() => vi.advanceTimersByTime(299));
    expect(result.current).toBe('a');
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe('b');
  });

  it('restarts the timer on each change (rapid typing yields one update)', () => {
    const { result, rerender } = renderHook(({ v }) => useDebouncedValue(v, 300), {
      initialProps: { v: '' },
    });
    rerender({ v: 'S' });
    act(() => vi.advanceTimersByTime(200));
    rerender({ v: 'SP' });
    act(() => vi.advanceTimersByTime(200));
    expect(result.current).toBe('');
    act(() => vi.advanceTimersByTime(100));
    expect(result.current).toBe('SP');
  });
});
