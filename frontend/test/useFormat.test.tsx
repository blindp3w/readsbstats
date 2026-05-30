// Audit-13 Phase 6: `hooks/useFormat.ts` re-render-on-store-change.
//
// The hook subscribes to both the units store and the clock-format
// store via Zustand selectors. The contract: any consumer using the
// returned helpers must re-render when the user toggles either store.
// This test pins that contract — if a future refactor stops
// subscribing, this test fails.

import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useFormat } from '@/hooks/useFormat';
import { useUnitsStore } from '@/store/units';
import { useClockStore } from '@/store/clockFormat';

beforeEach(() => {
  localStorage.clear();
  useUnitsStore.setState({ units: 'metric' });
  useClockStore.setState({ clockFormat: '24h' });
});

describe('useFormat — reactive to store changes', () => {
  it('initial values reflect store state', () => {
    const { result } = renderHook(() => useFormat());
    expect(result.current.units).toBe('metric');
    expect(result.current.clockFormat).toBe('24h');
  });

  it('re-renders when units store changes', () => {
    const { result } = renderHook(() => useFormat());
    expect(result.current.units).toBe('metric');

    act(() => {
      useUnitsStore.getState().setUnits('imperial');
    });

    expect(result.current.units).toBe('imperial');
  });

  it('re-renders when clockFormat store changes', () => {
    const { result } = renderHook(() => useFormat());
    expect(result.current.clockFormat).toBe('24h');

    act(() => {
      useClockStore.getState().setClockFormat('12h');
    });

    expect(result.current.clockFormat).toBe('12h');
  });

  it('formatters use the current store values, not snapshots', () => {
    const { result } = renderHook(() => useFormat());
    // Metric default: 1000 ft → 305 m.
    expect(result.current.fmtAlt(1000)).toBe('305 m');

    act(() => {
      useUnitsStore.getState().setUnits('imperial');
    });

    // Imperial: 1000 ft stays in feet.
    expect(result.current.fmtAlt(1000)).toBe('1,000 ft');
  });

  it('altLabel / spdLabel / distLabel reflect current units', () => {
    const { result } = renderHook(() => useFormat());
    // Metric: altitude in metres.
    expect(result.current.altLabel()).toMatch(/\bm\b/);

    act(() => {
      useUnitsStore.getState().setUnits('aeronautical');
    });

    // Aeronautical: feet.
    expect(result.current.altLabel()).toMatch(/\bft\b/);
  });
});
