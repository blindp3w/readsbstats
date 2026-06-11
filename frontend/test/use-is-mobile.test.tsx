// hooks/useIsMobile.ts — viewport breakpoint tracking via matchMedia.
//
// Contract: initial value reflects the current media-query state, a `change`
// event flips it without remount (orientation change / window resize), and
// the listener is removed on unmount.

import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useIsMobile } from '@/hooks/useIsMobile';

function installMatchMedia(initialMatches: boolean) {
  let matches = initialMatches;
  const listeners = new Set<() => void>();
  const mql = {
    get matches() {
      return matches;
    },
    media: '(max-width: 767px)',
    onchange: null,
    addEventListener: (_type: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_type: string, fn: () => void) => listeners.delete(fn),
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  };
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => mql,
  });
  return {
    listeners,
    setMatches(v: boolean) {
      matches = v;
      listeners.forEach((fn) => fn());
    },
  };
}

describe('useIsMobile', () => {
  it('is false on a desktop viewport', () => {
    installMatchMedia(false);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
  });

  it('is true when the query matches at mount', () => {
    installMatchMedia(true);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(true);
  });

  it('updates without remount when the breakpoint flips', () => {
    const mm = installMatchMedia(false);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);

    act(() => mm.setMatches(true));
    expect(result.current).toBe(true);

    act(() => mm.setMatches(false));
    expect(result.current).toBe(false);
  });

  it('removes its change listener on unmount', () => {
    const mm = installMatchMedia(false);
    const { unmount } = renderHook(() => useIsMobile());
    expect(mm.listeners.size).toBe(1);
    unmount();
    expect(mm.listeners.size).toBe(0);
  });
});
